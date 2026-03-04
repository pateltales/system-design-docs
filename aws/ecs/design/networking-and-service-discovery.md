# ECS Networking and Service Discovery — Deep Dive

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Network modes (awsvpc/bridge/host), ENI trunking, Service Connect, Cloud Map, load balancer integration

---

## Table of Contents

1. [Network Modes Overview](#1-network-modes-overview)
2. [awsvpc Mode — Deep Dive](#2-awsvpc-mode--deep-dive)
3. [Bridge Mode](#3-bridge-mode)
4. [Host Mode](#4-host-mode)
5. [Network Mode Comparison](#5-network-mode-comparison)
6. [Load Balancer Integration](#6-load-balancer-integration)
7. [Service Connect — Built-in Service Mesh](#7-service-connect--built-in-service-mesh)
8. [Cloud Map Service Discovery](#8-cloud-map-service-discovery)
9. [Service Interconnection Options](#9-service-interconnection-options)
10. [Design Decisions and Trade-offs](#10-design-decisions-and-trade-offs)
11. [Interview Angles](#11-interview-angles)

---

## 1. Network Modes Overview

| Mode | Linux EC2 | Windows EC2 | Fargate | Description |
|------|-----------|------------|---------|-------------|
| **awsvpc** | Yes | Yes | **Required** | Task gets its own ENI with private IP |
| **bridge** | Yes (default) | No | No | Docker virtual network on host |
| **host** | Yes | No | No | Container uses host's network directly |
| **none** | Yes | No | No | No external networking |
| **default** | No | Yes (default) | No | Windows NAT driver |

---

## 2. awsvpc Mode — Deep Dive

### 2.1 Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  EC2 Instance (or Fargate host)                               │
│                                                               │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  Task A                                              │     │
│  │  ┌───────────┐  ┌───────────┐                       │     │
│  │  │Container 1│  │Container 2│  (share Task ENI)     │     │
│  │  │ :8080     │  │ :9090     │                       │     │
│  │  └─────┬─────┘  └─────┬─────┘                       │     │
│  │        └───────┬───────┘                             │     │
│  │           ┌────▼─────┐                               │     │
│  │           │ Task ENI │  10.0.1.100                   │     │
│  │           │ sg-task-a│  (own security group)         │     │
│  │           └──────────┘                               │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                               │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  Task B                                              │     │
│  │  ┌───────────┐                                       │     │
│  │  │Container 3│                                       │     │
│  │  │ :8080     │  (same port as Task A — no conflict!) │     │
│  │  └─────┬─────┘                                       │     │
│  │   ┌────▼─────┐                                       │     │
│  │   │ Task ENI │  10.0.1.101                           │     │
│  │   │ sg-task-b│  (different security group)           │     │
│  │   └──────────┘                                       │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                               │
│  Primary ENI: 10.0.1.50 (instance management)                │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 Key Features

| Feature | Details |
|---------|---------|
| **ENI per task** | Each task gets a dedicated elastic network interface |
| **Private IP per task** | Own IPv4 (and optionally IPv6) address |
| **Security groups** | Per-task security groups (up to 5 per task) |
| **No port conflicts** | Multiple tasks can use the same port number |
| **VPC flow logs** | Traffic visible in VPC flow logs per task |
| **Network ACLs** | Subnet-level controls apply per task |

### 2.3 ENI Trunking

Without trunking, the number of tasks per instance is limited by the instance's ENI limit (e.g., c5.large supports 3 ENIs → only 2 tasks in awsvpc, since 1 ENI is for the instance itself).

**ENI trunking** solves this by using a single trunk ENI that multiplexes multiple task ENIs:

```
Without trunking:                    With trunking:
┌──────────────────────┐            ┌──────────────────────┐
│  c5.large (3 ENIs)   │            │  c5.large (3 ENIs)   │
│                      │            │                      │
│  Primary ENI (mgmt)  │            │  Primary ENI (mgmt)  │
│  Task ENI 1          │            │  Trunk ENI ──┐       │
│  Task ENI 2          │            │    ├── Branch ENI 1  │
│                      │            │    ├── Branch ENI 2  │
│  Max tasks: 2        │            │    ├── Branch ENI 3  │
└──────────────────────┘            │    ├── ...           │
                                    │    └── Branch ENI N  │
                                    │                      │
                                    │  Max tasks: ~10-120  │
                                    │  (instance dependent)│
                                    └──────────────────────┘
```

**How to enable**: Set the `awsvpcTrunking` account setting and use instances that support trunking (requires `ecs.capability.task-eni-trunking` attribute).

### 2.4 ENI Limits by Instance Type (Examples)

| Instance Type | ENIs Without Trunking | ENI Trunking Limit | Max awsvpc Tasks (Trunking) |
|---------------|----------------------|--------------------|-----------------------------|
| t3.micro | 2 | Not supported | 1 |
| c5.large | 3 | ~10 | ~9 |
| c5.xlarge | 4 | ~18 | ~17 |
| c5.2xlarge | 4 | ~38 | ~37 |
| c5.4xlarge | 8 | ~58 | ~57 |
| c5.18xlarge | 15 | ~120 | ~119 |

[INFERRED: Exact trunk limits vary by instance; check AWS docs for latest values]

### 2.5 awsvpc Configuration

```json
{
    "networkMode": "awsvpc",
    "networkConfiguration": {
        "awsvpcConfiguration": {
            "subnets": ["subnet-aaa", "subnet-bbb"],
            "securityGroups": ["sg-xxx"],
            "assignPublicIp": "ENABLED"
        }
    }
}
```

**Limits:**
- Up to 5 security groups per awsvpcConfiguration
- Up to 16 subnets per awsvpcConfiguration

---

## 3. Bridge Mode

### 3.1 Architecture

```
┌──────────────────────────────────────────────────────────┐
│  EC2 Instance                                             │
│                                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
│  │Container │  │Container │  │Container │               │
│  │  :8080   │  │  :8080   │  │  :3000   │               │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘               │
│       │              │              │                     │
│  ┌────▼──────────────▼──────────────▼────┐               │
│  │        Docker Bridge (docker0)         │               │
│  │        172.17.0.0/16                   │               │
│  └──────────────────┬────────────────────┘               │
│                     │ NAT                                 │
│                     │                                     │
│  ┌──────────────────▼────────────────────┐               │
│  │  Instance ENI: 10.0.1.50              │               │
│  │  Dynamic port mapping:                 │               │
│  │    Container 1: 10.0.1.50:32768       │               │
│  │    Container 2: 10.0.1.50:32769       │               │
│  │    Container 3: 10.0.1.50:3000        │               │
│  └───────────────────────────────────────┘               │
└──────────────────────────────────────────────────────────┘
```

### 3.2 Key Characteristics

| Feature | Details |
|---------|---------|
| **Port mapping** | Dynamic or static; multiple containers can use same internal port with different host ports |
| **Security groups** | Instance-level only (not per-task) |
| **IP address** | Containers share the instance's IP |
| **Docker network** | Uses `docker0` bridge (172.17.0.0/16) |
| **ALB integration** | Works with dynamic port mapping |
| **Default on Linux** | Yes (when no network mode specified) |

### 3.3 Dynamic Port Mapping

```json
{
    "containerDefinitions": [
        {
            "name": "web",
            "portMappings": [
                {
                    "containerPort": 8080,
                    "hostPort": 0
                }
            ]
        }
    ]
}
```

`hostPort: 0` means Docker assigns a random ephemeral port (32768–65535). ALB discovers the port via ECS registration.

---

## 4. Host Mode

### 4.1 Architecture

```
┌──────────────────────────────────────────────────────────┐
│  EC2 Instance                                             │
│                                                           │
│  ┌──────────────────────────────────────────────────┐    │
│  │  Container A: listening on :80                    │    │
│  │  (shares instance's network namespace)            │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
│  Instance ENI: 10.0.1.50                                  │
│  Port 80 → Container A  (direct mapping)                  │
│                                                           │
│  ⚠ Cannot run another task using port 80!                │
└──────────────────────────────────────────────────────────┘
```

### 4.2 Key Characteristics

| Feature | Details |
|---------|---------|
| **Port mapping** | Container port = host port (no remapping) |
| **Performance** | Highest (no NAT, no bridge overhead) |
| **Limitation** | Only one task per port per instance |
| **Security groups** | Instance-level only |
| **Use case** | Performance-critical single-task-per-instance |

---

## 5. Network Mode Comparison

| Feature | awsvpc | bridge | host |
|---------|--------|--------|------|
| **ENI** | Per task | Shared (instance) | Shared (instance) |
| **IP address** | Per task | Instance IP | Instance IP |
| **Security groups** | Per task | Per instance | Per instance |
| **Port conflicts** | No (each task has own IP) | Possible (dynamic mapping helps) | Yes (same port = conflict) |
| **Performance** | Good (slight ENI overhead) | Good | Best (no virtualization) |
| **Fargate support** | Yes (required) | No | No |
| **Multiple tasks/instance** | Limited by ENIs (trunking helps) | Limited by ports | Limited to 1 per port |
| **VPC flow logs** | Per task | Per instance | Per instance |
| **Recommended** | Yes (default for new apps) | Legacy | Niche |

### Trade-off: awsvpc vs bridge

```
                   ┌──────────────┐
    Security       │   awsvpc     │      awsvpc: per-task security groups,
    & Isolation    │              │      per-task IP, VPC flow logs per task
                   └──────┬───────┘
                          │
                          │ Trade-off
                          │
                   ┌──────▼───────┐
    Density &      │   bridge     │      bridge: more tasks per instance
    Simplicity     │              │      (no ENI limit), simpler networking
                   └──────────────┘
```

---

## 6. Load Balancer Integration

### 6.1 Supported Load Balancers

| LB Type | Layer | Use Case | awsvpc Target Type |
|---------|-------|----------|--------------------|
| **ALB** | 7 (HTTP/S) | Web services, APIs, path-based routing | `ip` |
| **NLB** | 4 (TCP/UDP) | High-perf TCP, gRPC, real-time | `ip` |
| **GLB** | 4 | Virtual appliances (firewall, IDS) | `ip` |

### 6.2 Target Types by Network Mode

| Network Mode | Target Type | Target |
|-------------|-------------|--------|
| awsvpc | `ip` | Task's ENI IP address |
| bridge | `instance` | Instance IP + dynamic port |
| host | `instance` | Instance IP + container port |

### 6.3 Multiple Target Groups

A single ECS service can register with **up to 5 target groups**. This enables:
- Internal ALB + External ALB (different routing)
- ALB (HTTP) + NLB (gRPC) on the same service
- Canary routing with weighted target groups

### 6.4 Health Check Integration

```
ALB Health Check → Task unhealthy → ECS drains and replaces task

┌─────────┐    Health check     ┌──────────────┐
│   ALB   │ ──────────────────► │  Task ENI    │
│         │    (HTTP 200?)      │  (10.0.1.100)│
│         │ ◄────────────────── │              │
└────┬────┘                     └──────────────┘
     │
     │ If unhealthy:
     │ 1. ALB stops sending traffic
     │ 2. ECS marks task unhealthy
     │ 3. Service scheduler replaces task
     │ 4. New task registers with ALB
```

---

## 7. Service Connect — Built-in Service Mesh

### 7.1 What Is Service Connect?

Service Connect is ECS's built-in service-to-service communication layer using Envoy proxy:

```
┌──────────────────────────────────────────────────────────────────┐
│                    ECS Cluster                                    │
│                    Namespace: "production"                        │
│                                                                   │
│  ┌────────────────────────┐    ┌────────────────────────┐       │
│  │  Frontend Service       │    │  Backend Service        │       │
│  │                         │    │                         │       │
│  │  ┌─────────┐           │    │  ┌─────────┐           │       │
│  │  │ App     │           │    │  │ App     │           │       │
│  │  │ Container│           │    │  │ Container│           │       │
│  │  │         │           │    │  │ :8080   │           │       │
│  │  │ curl    │           │    │  └────▲────┘           │       │
│  │  │ http:// │           │    │       │                 │       │
│  │  │ backend │           │    │  ┌────┴────┐           │       │
│  │  │ :8080   │           │    │  │ Envoy   │           │       │
│  │  └────┬────┘           │    │  │ Proxy   │ (server)  │       │
│  │       │                │    │  │ :8080   │           │       │
│  │  ┌────▼────┐           │    │  └─────────┘           │       │
│  │  │ Envoy   │           │    │                         │       │
│  │  │ Proxy   │ (client)  │    │  Endpoint:              │       │
│  │  │         │ ─────────────────► http://backend:8080   │       │
│  │  └─────────┘           │    │                         │       │
│  └────────────────────────┘    └────────────────────────┘       │
│                                                                   │
│  Service Connect handles:                                         │
│  ✓ Service discovery (no DNS needed)                              │
│  ✓ Load balancing (round-robin + outlier detection)              │
│  ✓ Metrics (connection performance reported to CloudWatch)       │
│  ✓ Optional TLS encryption (auto-rotated every 5 days)          │
└──────────────────────────────────────────────────────────────────┘
```

### 7.2 Service Types

| Type | Description | Has Endpoints? | Example |
|------|-------------|----------------|---------|
| **Client** | Can discover and connect to endpoints | No (only consumes) | Frontend, batch job |
| **Client-Server** | Reachable AND can discover others | Yes (exposes endpoints) | Backend API, database |

### 7.3 Key Concepts

| Concept | Description |
|---------|-------------|
| **Namespace** | Logical grouping of services; Cloud Map namespace under the hood |
| **Port name** | Task definition mapping of a name to a container port |
| **Discovery name** | Name registered in Cloud Map for the service |
| **Client alias** | DNS name and port used by client services to connect |
| **Endpoint** | `protocol://dns-name:port` (e.g., `http://backend:8080`) |

### 7.4 How It Works Under the Hood

1. ECS injects an **Envoy sidecar proxy** into each Service Connect-enabled task
2. Client proxy intercepts outbound connections to service endpoints
3. Proxy performs round-robin load balancing across healthy backend tasks
4. Outlier detection removes unhealthy backends from rotation
5. Both client and server proxies report metrics to CloudWatch
6. Optional TLS terminates at the proxy with auto-rotated certificates

### 7.5 Service Connect Features

| Feature | Details |
|---------|---------|
| Load balancing | Round-robin + outlier detection |
| TLS encryption | Optional, auto-rotated every 5 days (Private CA) |
| Metrics | Connection latency, success/failure rates → CloudWatch |
| Multi-cluster | Services across clusters in same region can share namespace |
| Cross-account | Namespace sharing via AWS RAM |
| Cost | No additional charge (proxy shares task resources) |

---

## 8. Cloud Map Service Discovery

### 8.1 How Cloud Map Works (Without Service Connect)

```
┌───────────────┐    Register    ┌──────────────────┐
│  ECS Task     │ ──────────────►│  AWS Cloud Map    │
│  (10.0.1.100) │                │                   │
│  Service: api │                │  api.prod.local   │
└───────────────┘                │  A record:        │
                                 │    10.0.1.100     │
┌───────────────┐    DNS query   │    10.0.1.101     │
│  Client Task  │ ──────────────►│    10.0.1.102     │
│               │                │                   │
│  resolve:     │ ◄──────────────│  Returns: random  │
│  api.prod.local               │  healthy IP       │
└───────────────┘                └──────────────────┘
```

### 8.2 DNS Record Types

| Record Type | What It Returns | Use Case |
|-------------|----------------|----------|
| **A record** | IP address of the task | Standard service discovery |
| **SRV record** | IP address + port number | Dynamic port mapping (bridge mode) |

**A records**: Best for awsvpc mode (known port)
**SRV records**: Needed for bridge mode (dynamic port)

### 8.3 Cloud Map Limitation

Services using Cloud Map have a reduced task limit: **1,000 tasks per service** (instead of 5,000) due to Cloud Map quotas.

---

## 9. Service Interconnection Options

### 9.1 Comparison

| Method | Complexity | Features | Best For |
|--------|-----------|----------|----------|
| **Service Connect** | Low | Discovery, LB, metrics, TLS, no DNS needed | Most ECS service-to-service |
| **Cloud Map (DNS)** | Medium | DNS-based discovery, A/SRV records | Cross-service-type discovery |
| **ALB/NLB** | Medium | L7/L4 routing, path-based, weighted | External traffic, advanced routing |
| **VPC Lattice** | Medium | Cross-VPC, cross-account, auth policies | Multi-account service networking |
| **App Mesh** | High | Full Envoy mesh, traffic policies | Complex routing/canary scenarios |

### 9.2 When to Use Each

| Scenario | Recommendation |
|----------|---------------|
| ECS service → ECS service (same cluster) | **Service Connect** |
| ECS service → ECS service (cross-cluster, same region) | **Service Connect** (shared namespace) |
| ECS service → External service | **ALB/NLB** or direct connection |
| Cross-account service communication | **VPC Lattice** or **Service Connect** (AWS RAM namespace) |
| External client → ECS service | **ALB/NLB** |
| Fine-grained traffic management (canary, retry policies) | **App Mesh** |

---

## 10. Design Decisions and Trade-offs

### 10.1 Why awsvpc Is Recommended

| Factor | awsvpc Advantage |
|--------|-----------------|
| Security | Per-task security groups — enforce least-privilege at the task level |
| Observability | Per-task VPC flow logs — trace traffic per workload |
| Simplicity | No port conflict management — every task gets its own IP |
| Fargate compatibility | Required for Fargate — consistent across EC2 and Fargate |
| ALB integration | Direct `ip` target type — no dynamic port discovery needed |

### 10.2 ENI Limit — The awsvpc Tax

The main downside of awsvpc is **ENI limits per instance**. Without trunking, a c5.large (3 ENIs) can only run 2 awsvpc tasks.

**Mitigations:**
- **ENI trunking**: Multiplexes branch ENIs over a trunk, increasing capacity 10-40x
- **Larger instances**: More ENIs available
- **Mixed strategy**: Use awsvpc for services needing per-task SGs, bridge for others

### 10.3 Service Connect vs Cloud Map

| Factor | Service Connect | Cloud Map (standalone) |
|--------|----------------|----------------------|
| Proxy overhead | Envoy sidecar in each task (shares task resources) | None (DNS only) |
| Load balancing | Built-in (round-robin + outlier detection) | DNS round-robin (TTL-based, less responsive) |
| Health checking | Active outlier detection | DNS health checks (slower) |
| Metrics | Automatic CloudWatch metrics per connection | None (build your own) |
| Configuration | ECS-native (task def + service def) | Separate Cloud Map config |
| Complexity | Lower (single config) | Higher (manage DNS, TTLs, health checks) |

---

## 11. Interview Angles

### 11.1 Key Questions

**Q: "Why does ECS recommend awsvpc over bridge mode?"**

awsvpc gives each task its own ENI with its own security group. This means you can enforce network-level least-privilege per task — task A can only talk to its database, task B can only talk to its cache. In bridge mode, all tasks on an instance share the instance's security group, so you can't differentiate. Additionally, awsvpc eliminates port conflicts and works with Fargate. The trade-off is ENI limits, which trunking mitigates.

**Q: "A customer has 100 tasks per instance but ENI trunking only supports 50. What do you recommend?"**

Options:
1. Use larger instances (more ENI trunk capacity)
2. Use bridge mode for tasks that don't need per-task security groups
3. Split across more instances (add instances to the cluster)
4. Mix awsvpc + bridge: critical services in awsvpc, others in bridge

**Q: "Service Connect vs just using an ALB for service-to-service?"**

ALB adds an extra hop and costs (per-ALB-hour + per-LCU). Service Connect uses an Envoy sidecar within the task — no extra infrastructure. Service Connect also provides outlier detection and automatic failover without DNS TTL delays. Use ALB for external traffic ingress; use Service Connect for internal service-to-service communication.

**Q: "What's the difference between A records and SRV records in Cloud Map?"**

A records return only IP addresses — sufficient for awsvpc (where the port is known). SRV records return IP + port — needed for bridge mode where the host port is dynamically assigned. In practice, if you're using awsvpc (recommended), A records are fine.

### 11.2 Numbers to Know

| Metric | Value |
|--------|-------|
| Security groups per awsvpcConfiguration | 5 |
| Subnets per awsvpcConfiguration | 16 |
| Target groups per service | 5 |
| Tasks per service (with Cloud Map) | 1,000 (vs 5,000 without) |
| TLS cert rotation (Service Connect) | Every 5 days |
| Network modes | 4 (awsvpc, bridge, host, none) + Windows default |
| ENI limit example (c5.large, no trunking) | 2 tasks |
| ENI limit example (c5.large, trunking) | ~9 tasks |

---

*Cross-references:*
- [Cluster Architecture](cluster-architecture.md) — Overall architecture, API overview
- [Fargate Architecture](fargate-architecture.md) — Why Fargate requires awsvpc
- [Task Placement](task-placement.md) — How ENI limits interact with placement
- [Deployment Strategies](deployment-strategies.md) — Load balancer integration during deployments
