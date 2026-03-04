# Lambda VPC Networking — Deep Dive

> Companion doc for [interview-simulation.md](interview-simulation.md)
> Covers: Hyperplane ENIs, pre-2019 vs post-2019 networking, NAT Gateway, VPC endpoints, security groups, cold start impact

---

## Table of Contents

1. [Default Lambda Networking (No VPC)](#1-default-lambda-networking-no-vpc)
2. [VPC-Connected Lambda — Architecture](#2-vpc-connected-lambda--architecture)
3. [The 2019 Networking Revolution](#3-the-2019-networking-revolution)
4. [Hyperplane ENI — Deep Dive](#4-hyperplane-eni--deep-dive)
5. [Internet Access for VPC-Connected Functions](#5-internet-access-for-vpc-connected-functions)
6. [VPC Endpoints — Accessing AWS Services Privately](#6-vpc-endpoints--accessing-aws-services-privately)
7. [Security Groups and Network ACLs](#7-security-groups-and-network-acls)
8. [IAM Permissions for VPC Configuration](#8-iam-permissions-for-vpc-configuration)
9. [Cold Start Impact of VPC Configuration](#9-cold-start-impact-of-vpc-configuration)
10. [Subnet Strategy and Multi-AZ](#10-subnet-strategy-and-multi-az)
11. [Advanced Scenarios](#11-advanced-scenarios)
12. [Monitoring and Troubleshooting](#12-monitoring-and-troubleshooting)
13. [Design Decisions and Trade-offs](#13-design-decisions-and-trade-offs)
14. [Interview Angles](#14-interview-angles)

---

## 1. Default Lambda Networking (No VPC)

### 1.1 The Lambda-Managed VPC

Every Lambda function — even those without VPC configuration — runs inside a VPC:

```
┌─────────────────────────────────────────────────────┐
│              Lambda-Managed VPC                      │
│              (Invisible to customer)                 │
│                                                      │
│   ┌──────────────────────┐                           │
│   │  Execution           │                           │
│   │  Environment         │ ───► Public Internet      │
│   │  (Your function)     │ ───► AWS Services (S3,    │
│   │                      │      DynamoDB, SQS, etc.) │
│   └──────────────────────┘                           │
│                                                      │
└─────────────────────────────────────────────────────┘
```

**Key points:**
- Lambda owns and manages this VPC — it is **not visible** to customers
- Functions have **full internet access** by default
- Functions can call any AWS service via public endpoints
- No ENIs are created in your account
- No subnet or security group configuration needed

### 1.2 When Is Default Networking Sufficient?

| Use Case | Default (No VPC) | VPC Required |
|----------|-------------------|--------------|
| Calling AWS service APIs (S3, DynamoDB, SQS) | Yes | No |
| Calling external HTTP APIs | Yes | No |
| Accessing RDS in a VPC | No | **Yes** |
| Accessing ElastiCache in a VPC | No | **Yes** |
| Accessing EC2 instances in a VPC | No | **Yes** |
| Accessing resources behind a VPN | No | **Yes** |
| Network-level compliance (no public internet) | No | **Yes** |

**Rule of thumb**: Only attach Lambda to a VPC when you need to access private resources inside that VPC.

---

## 2. VPC-Connected Lambda — Architecture

### 2.1 What Happens When You Attach to a VPC

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Lambda-Managed VPC                                │
│                    (Still exists, invisible)                         │
│                                                                     │
│   ┌──────────────────────┐                                          │
│   │  Execution           │                                          │
│   │  Environment         │                                          │
│   │  (Your function)     │                                          │
│   └──────────┬───────────┘                                          │
│              │                                                      │
│              │ VPC-to-VPC NAT [INFERRED]                            │
│              │                                                      │
└──────────────┼──────────────────────────────────────────────────────┘
               │
               │ Hyperplane ENI
               │ (Cross-account, in YOUR VPC)
               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       YOUR VPC                                       │
│                                                                      │
│  ┌─────────────────────────┐    ┌────────────────────────────────┐  │
│  │  Private Subnet A       │    │  Private Subnet B              │  │
│  │  ┌───────────────────┐  │    │  ┌───────────────────────┐    │  │
│  │  │ Hyperplane ENI    │  │    │  │ Hyperplane ENI        │    │  │
│  │  │ (auto-managed)    │  │    │  │ (auto-managed)        │    │  │
│  │  └────────┬──────────┘  │    │  └────────┬──────────────┘    │  │
│  │           │              │    │           │                    │  │
│  │    ┌──────▼──────┐      │    │    ┌──────▼──────┐            │  │
│  │    │ RDS         │      │    │    │ ElastiCache  │            │  │
│  │    │ Instance    │      │    │    │ Cluster      │            │  │
│  │    └─────────────┘      │    │    └──────────────┘            │  │
│  └─────────────────────────┘    └────────────────────────────────┘  │
│                                                                      │
│  No internet access by default! (No public IP assigned)              │
│  Need NAT Gateway for outbound internet                              │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 The Core Trade-off

When you attach a Lambda function to a VPC:

| Gained | Lost |
|--------|------|
| Access to private VPC resources (RDS, ElastiCache, EC2) | Direct internet access |
| Network-level isolation and compliance | Simple networking model |
| Security group control | Free internet access |
| VPC peering / Transit Gateway access | — |

**To restore internet access**: You need a NAT Gateway (see Section 5).

**To access AWS services privately**: Use VPC endpoints (see Section 6).

---

## 3. The 2019 Networking Revolution

### 3.1 Pre-2019 Model (Legacy)

Before September 2019, VPC networking worked like this:

```
PRE-2019: Per-Execution-Environment ENI

┌───────────┐     ┌──────────┐     ┌──────────┐
│ Function  │     │ Function │     │ Function │
│ Env 1     │     │ Env 2    │     │ Env 3    │
└─────┬─────┘     └────┬─────┘     └────┬─────┘
      │                 │                │
   ┌──▼──┐          ┌──▼──┐         ┌──▼──┐
   │ENI 1│          │ENI 2│         │ENI 3│
   └──┬──┘          └──┬──┘         └──┬──┘
      │                 │                │
      └─────────────────┼────────────────┘
                        │
                   YOUR VPC
```

**Problems:**
- **One ENI per execution environment** [INFERRED] — creating ENIs for every concurrent invocation
- **Cold starts of 10+ seconds** — ENI creation was synchronous and slow
- **ENI limits hit easily** — default 250 ENIs per region, exhausted quickly at scale
- **IP address exhaustion** — each ENI consumed a private IP from the subnet
- **Scale ceiling** — concurrency limited by ENI quotas and subnet IP space

### 3.2 Post-2019 Model (Current — Hyperplane)

Starting September 2019, AWS rolled out Hyperplane-based VPC networking:

```
POST-2019: Shared Hyperplane ENI

┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
│ Function  │  │ Function  │  │ Function  │  │ Function  │
│ Env 1     │  │ Env 2     │  │ Env 3     │  │ Env 4     │
└─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
      │              │              │              │
      │    VPC-to-VPC NAT (Hyperplane)             │
      │              │              │              │
      └──────────────┼──────────────┘              │
                     │                             │
              ┌──────▼──────┐               ┌──────▼──────┐
              │ Hyperplane  │               │ Hyperplane  │
              │ ENI (shared)│               │ ENI (shared)│
              │ 65K conns   │               │ 65K conns   │
              └──────┬──────┘               └──────┬──────┘
                     │                             │
                     └──────────────┬──────────────┘
                                   │
                              YOUR VPC
                        (subnet + security group)
```

**Improvements:**

| Aspect | Pre-2019 | Post-2019 |
|--------|----------|-----------|
| ENI per function | 1 per execution environment | 1 per (subnet, security-group) combo |
| Cold start addition | 10–30 seconds for ENI creation | ~1 second (ENI pre-created at deploy time) |
| ENI quota pressure | Severe (250 default) | Minimal (few shared ENIs) |
| IP address consumption | 1 IP per concurrent invocation | 1 IP per Hyperplane ENI (shared) |
| When ENI is created | At first invocation (synchronous) | At function create/update time (asynchronous) |
| Connection capacity | ~1,000 per ENI [INFERRED] | 65,000 per Hyperplane ENI |
| Scaling ceiling | Limited by ENI/IP quotas | Virtually unlimited |

### 3.3 How Hyperplane Works

[INFERRED] Hyperplane is AWS's internal network function virtualization platform (also used by NLB, NAT Gateway, PrivateLink):

1. **At deploy time**: Lambda creates Hyperplane ENIs in your VPC subnets (asynchronous, doesn't block invocation of existing versions)
2. **ENI sharing**: All functions with the same (subnet, security-group) pair share ENIs
3. **VPC-to-VPC NAT**: Hyperplane performs network address translation between the Lambda-managed VPC and your VPC
4. **Connection multiplexing**: A single Hyperplane ENI supports up to 65,000 connections
5. **Auto-scaling**: When connections exceed 65,000, Lambda creates additional ENIs automatically

---

## 4. Hyperplane ENI — Deep Dive

### 4.1 ENI Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│                  Hyperplane ENI Lifecycle                         │
│                                                                  │
│  Create/Update    Pending      Active        Idle (14+ days)    │
│  Function with ──► Function ──► Function ──► Lambda reclaims    │
│  VPC config        state       state         ENI                 │
│                    (minutes)   (ready)                           │
│                                                                  │
│                                  │                               │
│                                  ▼                               │
│                        Lambda may delete and                     │
│                        recreate ENIs for:                        │
│                        - Load balancing                          │
│                        - Health check failures                   │
│                        - Subnet/SG changes                       │
│                                                                  │
│  VPC config         ENI deletion                                 │
│  removed ──────────► (takes up to 20 minutes)                    │
│                      (only if no other function uses it)         │
└─────────────────────────────────────────────────────────────────┘
```

**State transitions:**

| State | Description | Duration |
|-------|-------------|----------|
| Pending | ENI being created/attached | Several minutes (first time) |
| Active | ENI ready, function can be invoked | Indefinite while in use |
| Inactive | ENI reclaimed after 14+ days of no invocations | Until next invocation |
| Re-Pending | ENI being recreated after Inactive state | Several minutes |

### 4.2 ENI Sharing Rules

**ENIs are shared when functions use the same:**
- Subnet ID
- Security group ID(s)

```
Example: 3 functions, 2 subnet/SG combinations

Function A: subnet-1, sg-alpha  ──┐
Function B: subnet-1, sg-alpha  ──┤── Share Hyperplane ENI #1
Function C: subnet-1, sg-alpha  ──┘

Function D: subnet-1, sg-beta   ──── Separate Hyperplane ENI #2

Function E: subnet-2, sg-alpha  ──── Separate Hyperplane ENI #3
```

**Best practice**: Use consistent subnet + security group combinations across functions to maximize ENI sharing and minimize resource usage.

### 4.3 Connection Capacity

| Metric | Value |
|--------|-------|
| Connections per Hyperplane ENI | 65,000 |
| Auto-scale trigger | Connection count exceeds 65,000 |
| New ENI creation | Automatic, based on traffic and concurrency |

**Capacity planning:**
- Each concurrent function invocation uses at least 1 connection through the ENI
- Functions with multiple outbound connections (e.g., database pool) consume multiple connections per invocation
- For 1,000 concurrent functions each making 10 DB connections: 10,000 connections → 1 ENI sufficient

### 4.4 ENI IP Address Behavior

- Each Hyperplane ENI gets a private IP from the subnet
- Functions do NOT get their own IP addresses (they share the ENI's IP)
- This means **all traffic from Lambda functions in a given ENI appears to come from the ENI's IP**
- Important for security group rules on target resources (e.g., RDS)

---

## 5. Internet Access for VPC-Connected Functions

### 5.1 The Problem

```
VPC-connected Lambda → NO public IP → NO internet access

Even in a public subnet, Lambda does NOT get a public IP.
This is different from EC2 (which can get a public/Elastic IP).
```

### 5.2 Solution: NAT Gateway

```
┌───────────────────────────────────────────────────────────────────┐
│                          YOUR VPC                                  │
│                                                                    │
│  ┌─────────────────────────┐    ┌─────────────────────────────┐  │
│  │  Private Subnet          │    │  Public Subnet               │  │
│  │                          │    │                               │  │
│  │  ┌──────────────────┐   │    │  ┌───────────────────┐       │  │
│  │  │ Hyperplane ENI   │   │    │  │  NAT Gateway      │       │  │
│  │  │ (Lambda traffic) │   │    │  │  (Elastic IP)     │       │  │
│  │  └────────┬─────────┘   │    │  └─────────┬─────────┘       │  │
│  │           │              │    │            │                  │  │
│  │  Route table:            │    │  Route table:                │  │
│  │  0.0.0.0/0 → NAT GW ───────────►         │                  │  │
│  │                          │    │  0.0.0.0/0 → IGW  ──────────┼──┼──► Internet
│  └─────────────────────────┘    └─────────────────────────────┘  │
│                                                                    │
│                              Internet Gateway (IGW)                │
└───────────────────────────────────────────────────────────────────┘
```

**Setup requirements:**
1. Place Lambda in **private subnets**
2. Create a **NAT Gateway** in a **public subnet**
3. Add a **route** in the private subnet's route table: `0.0.0.0/0 → NAT Gateway`
4. The public subnet must have a route: `0.0.0.0/0 → Internet Gateway`
5. NAT Gateway needs an **Elastic IP**

**Cost implications:**
- NAT Gateway: ~$0.045/hour (~$32/month per AZ)
- Data processing: $0.045/GB through NAT Gateway
- For multi-AZ: multiply by number of AZs

### 5.3 Alternative: VPC Endpoints (Cheaper for AWS Services)

If your Lambda only needs to access AWS services (not the public internet), VPC endpoints are cheaper:

```
┌────────────────────────────────────────────────────┐
│                  YOUR VPC                           │
│                                                     │
│  ┌──────────────────┐    ┌──────────────────────┐  │
│  │ Hyperplane ENI   │    │ VPC Endpoint         │  │
│  │ (Lambda)         │───►│ (com.amazonaws.      │  │
│  └──────────────────┘    │  region.s3)          │  │
│                          └──────────┬───────────┘  │
│                                     │               │
└─────────────────────────────────────┼───────────────┘
                                      │
                            AWS Internal Network
                            (never touches internet)
                                      │
                               ┌──────▼──────┐
                               │  Amazon S3  │
                               └─────────────┘
```

**No NAT Gateway needed for AWS service access when using VPC endpoints.**

### 5.4 Decision Matrix: NAT Gateway vs VPC Endpoints

| Need | Solution | Cost |
|------|----------|------|
| Access AWS services only | VPC endpoints (Gateway type for S3/DynamoDB: free; Interface type for others: ~$0.01/hr) | Lower |
| Access public internet + AWS services | NAT Gateway + optional VPC endpoints | Higher |
| Access only private VPC resources | Neither (just VPC config) | No extra |
| Access public APIs (Stripe, Twilio, etc.) | NAT Gateway | NAT costs |

---

## 6. VPC Endpoints — Accessing AWS Services Privately

### 6.1 Two Types of VPC Endpoints

| Type | Gateway Endpoint | Interface Endpoint (PrivateLink) |
|------|-----------------|----------------------------------|
| **Supported services** | S3, DynamoDB only | 100+ services (Lambda, SQS, SNS, etc.) |
| **How it works** | Route table entry | ENI in your subnet |
| **Cost** | **Free** | ~$0.01/hr + $0.01/GB |
| **DNS** | Route table based | Private DNS resolution |
| **Cross-region** | No | No |
| **Security** | Endpoint policy | Endpoint policy + security groups |

### 6.2 Gateway Endpoint (S3 and DynamoDB)

```
Route table entry: pl-xxxxxxxx (S3 prefix list) → vpce-xxxxxxxx

Lambda → Hyperplane ENI → Route table → Gateway Endpoint → S3
                                    (stays on AWS backbone)
```

- **Free** — no additional charges
- Configured per route table, not per subnet
- Cannot be accessed from on-premises (VPN/Direct Connect)

### 6.3 Interface Endpoint (PrivateLink)

```
Lambda → Hyperplane ENI → Interface Endpoint ENI → AWS Service
                          (private IP in your subnet)
```

**Lambda-specific interface endpoint:**
- Service name: `com.amazonaws.<region>.lambda`
- Allows invoking Lambda functions from within VPC without internet access
- Supports all Lambda API operations (Invoke, CreateFunction, etc.)

**Creating a Lambda interface endpoint:**
```bash
aws ec2 create-vpc-endpoint \
    --vpc-id vpc-ec43eb89 \
    --vpc-endpoint-type Interface \
    --service-name com.amazonaws.us-east-1.lambda \
    --subnet-id subnet-abababab \
    --security-group-id sg-1a2b3c4d
```

**Endpoint policy example (restrict to specific function):**
```json
{
    "Statement": [
        {
            "Principal": {
                "AWS": "arn:aws:iam::111122223333:user/MyUser"
            },
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": [
                "arn:aws:lambda:us-east-2:123456789012:function:my-function",
                "arn:aws:lambda:us-east-2:123456789012:function:my-function:*"
            ]
        }
    ]
}
```

### 6.4 Common VPC Endpoint Configurations for Lambda

| AWS Service | Endpoint Type | Service Name | Use Case |
|-------------|---------------|-------------|----------|
| S3 | Gateway (free) | `com.amazonaws.<region>.s3` | Reading/writing data |
| DynamoDB | Gateway (free) | `com.amazonaws.<region>.dynamodb` | Table operations |
| SQS | Interface | `com.amazonaws.<region>.sqs` | Queue operations |
| SNS | Interface | `com.amazonaws.<region>.sns` | Publishing notifications |
| Secrets Manager | Interface | `com.amazonaws.<region>.secretsmanager` | Retrieving secrets |
| KMS | Interface | `com.amazonaws.<region>.kms` | Encryption operations |
| STS | Interface | `com.amazonaws.<region>.sts` | Assuming roles |
| Lambda | Interface | `com.amazonaws.<region>.lambda` | Invoking other functions |
| CloudWatch Logs | Interface | `com.amazonaws.<region>.logs` | Writing logs |

### 6.5 Private DNS and VPC Endpoints

When you enable **private DNS** on an interface endpoint:
- The default public DNS name (e.g., `sqs.us-east-1.amazonaws.com`) resolves to the endpoint's private IP
- No code changes needed — SDK calls automatically route through the endpoint
- **Recommended** for all interface endpoints

Without private DNS:
- You must use the endpoint-specific DNS name (e.g., `vpce-xxx.sqs.us-east-1.vpce.amazonaws.com`)
- Requires SDK configuration changes

---

## 7. Security Groups and Network ACLs

### 7.1 Security Groups for Lambda

Security groups attached to Lambda control **outbound traffic** from the function:

```
┌──────────────────────────────────────────────────────────┐
│  Lambda Security Group (sg-lambda)                        │
│                                                           │
│  Outbound Rules:                                          │
│  ┌──────────────┬──────────┬──────────┬────────────────┐ │
│  │ Type         │ Protocol │ Port     │ Destination    │ │
│  ├──────────────┼──────────┼──────────┼────────────────┤ │
│  │ HTTPS        │ TCP      │ 443      │ 0.0.0.0/0     │ │
│  │ PostgreSQL   │ TCP      │ 5432     │ sg-rds         │ │
│  │ Redis        │ TCP      │ 6379     │ sg-cache       │ │
│  └──────────────┴──────────┴──────────┴────────────────┘ │
│                                                           │
│  Inbound Rules:                                           │
│  (Typically NONE needed - Lambda initiates connections)   │
│  (Return traffic is automatically allowed by SG state)    │
└──────────────────────────────────────────────────────────┘
```

**Key points:**
- Lambda initiates outbound connections — focus on **outbound rules**
- Security groups are **stateful** — return traffic is automatically allowed
- Inbound rules are generally unnecessary (Lambda doesn't listen on ports)
- Multiple functions can share the same security group → share ENIs

### 7.2 Security Groups on Target Resources

Target resources (RDS, ElastiCache, etc.) must allow inbound from Lambda's security group:

```
RDS Security Group (sg-rds):
  Inbound: TCP 5432 from sg-lambda  ✓  (allows Lambda to connect)
```

### 7.3 Network ACLs

NACLs apply at the subnet level and are **stateless** (must allow both inbound and outbound):

| Rule | Direction | Protocol | Port | Source/Dest | Action |
|------|-----------|----------|------|-------------|--------|
| 100 | Inbound | TCP | 1024-65535 | 0.0.0.0/0 | Allow (return traffic) |
| 100 | Outbound | TCP | 443 | 0.0.0.0/0 | Allow (HTTPS) |
| 200 | Outbound | TCP | 5432 | 10.0.0.0/16 | Allow (RDS) |

**Best practice**: Use security groups for Lambda traffic control. NACLs add complexity and are stateless — use only when required by compliance.

---

## 8. IAM Permissions for VPC Configuration

### 8.1 Execution Role Permissions

The Lambda execution role needs EC2 permissions to manage ENIs:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:CreateNetworkInterface",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeSubnets",
                "ec2:DeleteNetworkInterface",
                "ec2:AssignPrivateIpAddresses",
                "ec2:UnassignPrivateIpAddresses"
            ],
            "Resource": "*"
        }
    ]
}
```

**AWS managed policy**: `AWSLambdaVPCAccessExecutionRole`

### 8.2 Security Concern: Function Code Using ENI Permissions

The EC2 permissions are on the execution role, which means the function code could theoretically call EC2 APIs (CreateNetworkInterface, etc.). To prevent this:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Deny",
            "Action": [
                "ec2:CreateNetworkInterface",
                "ec2:DeleteNetworkInterface",
                "ec2:DescribeNetworkInterfaces",
                "ec2:DescribeSubnets",
                "ec2:DetachNetworkInterface",
                "ec2:AssignPrivateIpAddresses",
                "ec2:UnassignPrivateIpAddresses"
            ],
            "Resource": "*",
            "Condition": {
                "ArnEquals": {
                    "lambda:SourceFunctionArn": [
                        "arn:aws:lambda:us-west-2:123456789012:function:my_function"
                    ]
                }
            }
        }
    ]
}
```

This uses the `lambda:SourceFunctionArn` condition key to deny EC2 API calls when made **from function code**, while still allowing the Lambda service itself to manage ENIs.

### 8.3 IAM Condition Keys for VPC Governance

| Condition Key | Purpose | Example |
|---------------|---------|---------|
| `lambda:VpcIds` | Restrict which VPCs functions can attach to | Enforce all functions in production VPC |
| `lambda:SubnetIds` | Restrict which subnets | Enforce private subnets only |
| `lambda:SecurityGroupIds` | Restrict which security groups | Enforce approved SG configurations |

**Example: Enforce all functions must be VPC-connected:**
```json
{
    "Sid": "EnforceVPCFunction",
    "Action": [
        "lambda:CreateFunction",
        "lambda:UpdateFunctionConfiguration"
    ],
    "Effect": "Deny",
    "Resource": "*",
    "Condition": {
        "Null": { "lambda:VpcIds": "true" }
    }
}
```

**Example: Restrict to specific subnets:**
```json
{
    "Sid": "EnforceSpecificSubnets",
    "Action": [
        "lambda:CreateFunction",
        "lambda:UpdateFunctionConfiguration"
    ],
    "Effect": "Allow",
    "Resource": "*",
    "Condition": {
        "ForAllValues:StringEquals": {
            "lambda:SubnetIds": ["subnet-1", "subnet-2"]
        }
    }
}
```

---

## 9. Cold Start Impact of VPC Configuration

### 9.1 Pre-2019 Cold Start Impact

Before the 2019 Hyperplane improvement:

```
Cold Start Timeline (Pre-2019):
├── Create ENI ─────────────────── 10-30 seconds
├── Attach ENI ──────────────────── 1-2 seconds
├── Download code ──────────────── 0.5-2 seconds
├── Start runtime ──────────────── 0.5-5 seconds
├── Run init code ──────────────── 0-10 seconds
└── Total cold start ────────────── 12-47+ seconds
```

This was the **#1 complaint** about Lambda VPC configuration and the primary reason many teams avoided VPC-connected Lambda.

### 9.2 Post-2019 Cold Start Impact

After Hyperplane:

```
Cold Start Timeline (Post-2019):
├── ENI already exists (created at deploy time)
├── VPC-to-VPC NAT setup ──────── ~1 second [INFERRED]
├── Download code ──────────────── 0.5-2 seconds
├── Start runtime ──────────────── 0.5-5 seconds
├── Run init code ──────────────── 0-10 seconds
└── Total cold start ────────────── 2-18 seconds (same as non-VPC!)
```

**The VPC penalty is essentially eliminated** in the post-2019 model. Cold starts for VPC-connected functions are now comparable to non-VPC functions.

### 9.3 The Inactive State Edge Case

If a function is not invoked for **14+ days**:
1. Lambda reclaims the Hyperplane ENI
2. Function enters **Inactive** state
3. Next invocation fails (function needs to re-create ENI)
4. Function enters **Pending** state
5. New ENI is created (takes several minutes)
6. Function becomes **Active** again

**Mitigation**: Use a scheduled EventBridge rule to invoke the function periodically if it's rarely used but must be available.

### 9.4 Operations Blocked During ENI Provisioning

| Operation | Blocked During ENI Provisioning? |
|-----------|--------------------------------|
| Invoke (new function) | Yes — must wait |
| Invoke (existing version) | No — previous version still works |
| Create new version | Yes |
| Update function code | Yes |
| Update configuration (non-VPC) | Depends |

---

## 10. Subnet Strategy and Multi-AZ

### 10.1 Recommended Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                          YOUR VPC                            │
│                                                              │
│  AZ-a                          AZ-b                          │
│  ┌────────────────────┐       ┌────────────────────┐        │
│  │ Public Subnet A     │       │ Public Subnet B     │        │
│  │ ┌────────────────┐ │       │ ┌────────────────┐  │        │
│  │ │ NAT Gateway A  │ │       │ │ NAT Gateway B  │  │        │
│  │ └────────────────┘ │       │ └────────────────┘  │        │
│  └────────────────────┘       └────────────────────┘        │
│                                                              │
│  ┌────────────────────┐       ┌────────────────────┐        │
│  │ Private Subnet A   │       │ Private Subnet B    │        │
│  │ ┌────────────────┐ │       │ ┌────────────────┐  │        │
│  │ │ Hyperplane ENI │ │       │ │ Hyperplane ENI  │  │        │
│  │ │ (Lambda)       │ │       │ │ (Lambda)        │  │        │
│  │ └────────────────┘ │       │ └────────────────┘  │        │
│  │                    │       │                      │        │
│  │ ┌────────────────┐ │       │ ┌────────────────┐  │        │
│  │ │ RDS Primary    │ │       │ │ RDS Standby    │  │        │
│  │ └────────────────┘ │       │ └────────────────┘  │        │
│  └────────────────────┘       └────────────────────┘        │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 10.2 Why Multiple Subnets?

| Reason | Explanation |
|--------|-------------|
| **High availability** | If one AZ fails, Lambda can use ENIs in the other AZ |
| **IP address capacity** | More subnets = more available IPs for ENIs |
| **NAT Gateway HA** | One NAT Gateway per AZ (if AZ-a fails, AZ-b NAT still works) |

### 10.3 Subnet Sizing

Hyperplane ENIs require far fewer IPs than the pre-2019 model:

| Scenario | Pre-2019 IPs Needed | Post-2019 IPs Needed |
|----------|--------------------|--------------------|
| 100 concurrent functions, same SG | ~100 | 1–2 |
| 1,000 concurrent functions, same SG | ~1,000 | 1–2 (65K connections per ENI) |
| 1,000 concurrent, 10 different SGs | ~1,000 | ~10–20 |

**Recommendation**: A /24 subnet (251 usable IPs) is more than sufficient for most Lambda workloads with the Hyperplane model.

---

## 11. Advanced Scenarios

### 11.1 Cross-Account VPC Access

Lambda can access resources in VPCs belonging to other AWS accounts using:

| Method | How It Works | Use Case |
|--------|-------------|----------|
| **VPC Peering** | Direct VPC-to-VPC route | Accessing RDS in another account |
| **Transit Gateway** | Hub-and-spoke routing | Multiple accounts/VPCs |
| **PrivateLink** | Service endpoint across accounts | Exposing a service to Lambda |
| **VPC Lattice** | Application-layer networking | Service-to-service communication |

### 11.2 VPC-Connected Lambda + Event Source Mappings

When Lambda polls Kafka/MQ (which live in VPCs), the event source mapping **also uses the VPC configuration**:

```
┌──────────────────────────────────────────────────┐
│                    YOUR VPC                       │
│                                                   │
│  ┌────────────────┐    ┌──────────────────────┐  │
│  │ Event Pollers  │───►│ MSK Cluster          │  │
│  │ (Lambda ESM)   │    │ (Kafka brokers)      │  │
│  │                │◄───│                       │  │
│  └───────┬────────┘    └──────────────────────┘  │
│          │                                        │
│          │ Invoke (with batch)                    │
│          ▼                                        │
│  ┌────────────────┐                               │
│  │ Lambda Function│                               │
│  │ (VPC-connected)│                               │
│  └────────────────┘                               │
│                                                   │
└──────────────────────────────────────────────────┘
```

**Key**: The Lambda function must be VPC-connected with subnets that can reach the Kafka brokers.

### 11.3 VPC Tenancy Limitation

Lambda **cannot** connect to VPCs with **dedicated instance tenancy**.

**Workaround:**
1. Create a second VPC with **default tenancy**
2. Peer the dedicated VPC to the default tenancy VPC
3. Connect Lambda to the default tenancy VPC
4. Route traffic through the peering connection to the dedicated VPC

### 11.4 IPv6 / Dual-Stack Support

| Feature | Support |
|---------|---------|
| IPv6 in VPC-connected Lambda | Yes (opt-in) |
| IPv6-only subnets | **Not supported** |
| Dual-stack subnets (IPv4 + IPv6) | Supported |
| IPv6 for non-VPC functions | **Not supported** |
| Configuration | `Ipv6AllowedForDualStack: true` |

Requirements: All selected subnets must have both IPv4 and IPv6 CIDR blocks.

---

## 12. Monitoring and Troubleshooting

### 12.1 Common VPC Networking Issues

| Symptom | Likely Cause | Solution |
|---------|--------------|----------|
| Function times out connecting to RDS | Security group blocks outbound | Add outbound rule for port 5432/3306 to RDS SG |
| Function can't reach internet | No NAT Gateway | Add NAT Gateway + route table entry |
| Function stuck in Pending state | ENI creation in progress | Wait (can take minutes); check ENI limits |
| Function in Inactive state | No invocations for 14+ days | Invoke to re-activate; add keep-alive schedule |
| `ENILimitReachedException` | Too many unique subnet/SG combos | Consolidate to fewer combinations |
| DNS resolution fails | No DNS support in VPC | Enable DNS hostnames and DNS resolution in VPC |
| Can't reach AWS services | No internet and no VPC endpoint | Add NAT Gateway or VPC endpoints |
| Intermittent connection drops | ENI health check recreation | Normal behavior; implement connection retry logic |

### 12.2 Key Metrics

| Metric | Source | What It Tells You |
|--------|--------|-------------------|
| Function duration | CloudWatch Lambda metrics | If VPC connections are slow |
| Function errors | CloudWatch Lambda metrics | Connection timeouts |
| ENI count | EC2 ENI inventory | How many ENIs Lambda is using |
| NAT Gateway bytes processed | CloudWatch NAT metrics | Outbound data volume and cost |
| VPC endpoint bytes processed | CloudWatch VPC Endpoint metrics | Private service access volume |

### 12.3 Debugging Connectivity

**Step 1: Verify security groups**
```bash
# Check Lambda function's security group outbound rules
aws ec2 describe-security-groups --group-ids sg-lambda-id

# Check target resource's security group inbound rules
aws ec2 describe-security-groups --group-ids sg-rds-id
```

**Step 2: Verify route tables**
```bash
# Check the route table for Lambda's subnet
aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=subnet-id"
```

**Step 3: Verify ENI exists**
```bash
# List ENIs created by Lambda
aws ec2 describe-network-interfaces \
    --filters "Name=requester-id,Values=*lambda*" \
    --query "NetworkInterfaces[].{ID:NetworkInterfaceId,Subnet:SubnetId,SG:Groups[0].GroupId,Status:Status}"
```

---

## 13. Design Decisions and Trade-offs

### 13.1 Why Lambda Uses VPC-to-VPC NAT Instead of Placing Functions Directly in Your VPC

| Factor | Direct Placement (Rejected) | VPC-to-VPC NAT (Chosen) |
|--------|---------------------------|------------------------|
| Isolation | Function code runs in your VPC — security risk | Function runs in Lambda VPC — strong isolation |
| Multi-tenancy | Difficult — each customer's VPC is different | Easy — Lambda manages one fleet of workers |
| ENI overhead | 1 ENI per execution environment | 1 ENI per (subnet, SG) pair |
| IP consumption | Scales with concurrency | Minimal |
| Cold start | ENI creation per invocation | ENI pre-created at deploy time |
| Control plane | Complex (manage ENIs in customer VPCs) | Simpler (Hyperplane abstracts it) |

### 13.2 Why 65,000 Connections Per ENI?

The 65,000 limit matches the TCP/UDP port range (65,535 ephemeral ports). Each connection through the Hyperplane ENI uses a unique source port for NAT, so the theoretical maximum is ~65,000 concurrent connections per ENI.

### 13.3 Why Not Give Lambda Functions Public IPs?

| Factor | Reason |
|--------|--------|
| Security | Public IPs are directly routable from the internet — attack surface |
| Cost | IPv4 addresses are scarce and increasingly expensive |
| Design | Lambda functions are ephemeral; attaching/detaching public IPs adds latency |
| Alternative | NAT Gateway provides outbound internet without exposing Lambda to inbound traffic |

### 13.4 Why Private Subnets, Not Public?

Even though Lambda could be placed in a public subnet:
- Lambda **never gets a public IP**, so public subnet provides no benefit
- Private subnet + NAT Gateway gives outbound internet without inbound exposure
- Security compliance: many organizations require all compute in private subnets

### 13.5 Cost of VPC Networking

| Component | Cost | Notes |
|-----------|------|-------|
| Hyperplane ENI | Free | Managed by Lambda |
| NAT Gateway | ~$32/month/AZ + $0.045/GB | Significant for high-volume |
| VPC Gateway Endpoint (S3/DDB) | Free | Always use for S3/DDB |
| VPC Interface Endpoint | ~$7/month/AZ + $0.01/GB | Per endpoint |
| Elastic IP (for NAT) | ~$3.65/month (if idle) | Associated with NAT GW |

**Cost optimization**: Use VPC Gateway endpoints (free) for S3 and DynamoDB. Only use NAT Gateway if you need external internet access.

---

## 14. Interview Angles

### 14.1 Likely Questions

**Q: "A customer's VPC-connected Lambda function can't connect to RDS. How do you troubleshoot?"**

Systematic approach:
1. **Security groups**: Does Lambda's SG allow outbound on port 3306/5432? Does RDS SG allow inbound from Lambda's SG?
2. **Subnets**: Is Lambda in a subnet that can route to RDS's subnet? (Same VPC? Peered? Transit Gateway?)
3. **Route tables**: Do the subnets have proper route table entries?
4. **NACLs**: Are network ACLs blocking traffic? (Stateless — check both directions)
5. **DNS**: Can Lambda resolve the RDS endpoint? (VPC DNS enabled?)
6. **Function timeout**: Is the function timing out before the connection succeeds?

**Q: "How did the 2019 VPC networking improvement work?"**

Pre-2019: Each execution environment created its own ENI in the customer's VPC. This meant:
- ENI creation (10-30s) happened during cold start
- ENI quotas and IP addresses scaled with concurrency
- VPC cold starts were 10x worse than non-VPC

Post-2019: Lambda introduced Hyperplane ENIs that are shared:
- ENI created at deploy time (not at invocation time)
- One ENI per (subnet, security group) combination, supporting 65,000 connections
- VPC-to-VPC NAT tunnels traffic between Lambda's VPC and the customer's VPC
- Cold start penalty essentially eliminated

**Q: "Should this function be VPC-connected?"**

Only if it needs to access private VPC resources (RDS, ElastiCache, EC2, etc.). If it only calls AWS service APIs (S3, DynamoDB, SQS) or external HTTP APIs, don't attach to a VPC — it adds complexity (NAT Gateway cost, subnet management, security groups) with no benefit.

**Q: "Lambda function needs to call a third-party API from inside a VPC. What's the cheapest way?"**

If the function also needs to call AWS services:
1. Use free VPC Gateway endpoints for S3/DynamoDB
2. Use VPC Interface endpoints for other AWS services ($7/month/AZ each)
3. Use NAT Gateway only for the third-party API traffic

If the function only needs the third-party API:
- NAT Gateway is the only option (~$32/month/AZ minimum)
- Consider whether VPC is truly necessary

**Q: "How many concurrent Lambda executions can run in a /24 subnet?"**

Post-2019 (Hyperplane): A /24 subnet has ~251 usable IPs. Each Hyperplane ENI uses 1 IP and supports 65,000 connections. With 1 security group, you need ~1 ENI, supporting up to 65,000 concurrent functions. Even with 10 different security groups, you'd use ~10 IPs — still far below the /24 capacity. The practical limit is Lambda's concurrency quota (1,000 default), not subnet IP space.

### 14.2 Numbers to Know

| Metric | Value |
|--------|-------|
| Connections per Hyperplane ENI | 65,000 |
| ENI creation time (post-2019) | At deploy time (not cold start) |
| ENI creation time (pre-2019) | 10–30 seconds (during cold start) |
| Inactive threshold | 14 days with no invocations |
| ENI deletion time | Up to 20 minutes after VPC config removal |
| NAT Gateway cost | ~$0.045/hour + $0.045/GB |
| Gateway endpoint cost (S3/DDB) | Free |
| Interface endpoint cost | ~$0.01/hour + $0.01/GB |
| VPC tenancy limitation | Cannot connect to dedicated tenancy VPCs |
| IPv6 support | Dual-stack only (no IPv6-only subnets) |
| ENI sharing rule | Same subnet + same security group(s) |

### 14.3 Red Flags in Interviews

| Red Flag | Why It's Wrong |
|----------|---------------|
| "VPC Lambda is slower because of ENI creation" | True pre-2019, not post-2019 (Hyperplane eliminated this) |
| "Lambda gets a public IP in a public subnet" | Lambda never gets a public IP in any subnet |
| "Just put Lambda in a public subnet for internet access" | Won't work — need NAT Gateway in a public subnet |
| "VPC endpoints replace NAT Gateway entirely" | Only for AWS services, not public internet APIs |
| "Each Lambda invocation gets its own ENI" | Post-2019: shared ENIs, one per (subnet, SG) combo |
| "VPC-connected Lambda can access the internet by default" | VPC-connected Lambda loses internet access |

---

*Cross-references:*
- [Execution Environment Lifecycle](execution-environment-lifecycle.md) — Cold start impact, Init phases
- [Worker Fleet and Placement](worker-fleet-and-placement.md) — How Lambda-managed VPC hosts worker fleet
- [Invocation Models](invocation-models.md) — Event source mappings that require VPC (Kafka, MQ)
- [Firecracker Deep Dive](firecracker-deep-dive.md) — Network namespace isolation within MicroVMs
