# Amazon S3 — Security & Access Control Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores S3's security model including authentication, authorization, encryption, and compliance features.

---

## Table of Contents

1. [S3 Security Philosophy — Defense in Depth](#1-s3-security-philosophy--defense-in-depth)
2. [Authentication — AWS Signature Version 4 (SigV4)](#2-authentication--aws-signature-version-4-sigv4)
3. [Authorization — IAM Policies, Bucket Policies, ACLs](#3-authorization--iam-policies-bucket-policies-acls)
4. [Policy Evaluation Logic](#4-policy-evaluation-logic)
5. [S3 Block Public Access](#5-s3-block-public-access)
6. [Encryption at Rest](#6-encryption-at-rest)
7. [Encryption in Transit](#7-encryption-in-transit)
8. [S3 Object Lock — WORM Compliance](#8-s3-object-lock--worm-compliance)
9. [MFA Delete](#9-mfa-delete)
10. [S3 Access Points](#10-s3-access-points)
11. [VPC Endpoints for S3](#11-vpc-endpoints-for-s3)
12. [Presigned URLs — Temporary Access](#12-presigned-urls--temporary-access)
13. [Logging & Audit](#13-logging--audit)
14. [Common Security Anti-Patterns & Mitigations](#14-common-security-anti-patterns--mitigations)
15. [Cross-Account Access Patterns](#15-cross-account-access-patterns)
16. [Interview Tips — Security Questions](#16-interview-tips--security-questions)

---

## 1. S3 Security Philosophy — Defense in Depth

Amazon S3 stores trillions of objects and handles millions of requests per second. It is the
backbone of data storage for nearly every AWS customer. Because S3 stores the world's data,
security is treated as an absolute, non-negotiable requirement.

### 1.1 The Defense-in-Depth Model

S3 security is not a single gate. It is a series of concentric walls, each providing an
independent layer of protection. If one layer is misconfigured, the others still defend.

```
┌─────────────────────────────────────────────────────────────────┐
│                         Network Layer                           │
│  VPC Endpoints, PrivateLink, Security Groups, NACLs             │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    Identity Layer                        │    │
│  │  IAM Policies, STS Temporary Credentials, MFA            │    │
│  │  ┌─────────────────────────────────────────────────┐     │    │
│  │  │               Resource Layer                     │     │    │
│  │  │  Bucket Policies, ACLs, Access Points            │     │    │
│  │  │  ┌─────────────────────────────────────────┐     │     │    │
│  │  │  │           Encryption Layer               │     │     │    │
│  │  │  │  SSE-S3, SSE-KMS, SSE-C, TLS in transit │     │     │    │
│  │  │  │  ┌─────────────────────────────────┐     │     │     │    │
│  │  │  │  │       Monitoring Layer           │     │     │     │    │
│  │  │  │  │  CloudTrail, Access Logs,        │     │     │     │    │
│  │  │  │  │  S3 Inventory, Macie             │     │     │     │    │
│  │  │  │  │  ┌─────────────────────────┐     │     │     │     │    │
│  │  │  │  │  │       DATA              │     │     │     │     │    │
│  │  │  │  │  └─────────────────────────┘     │     │     │     │    │
│  │  │  │  └─────────────────────────────────┘     │     │     │    │
│  │  │  └─────────────────────────────────────────┘     │     │    │
│  │  └─────────────────────────────────────────────────┘     │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Default Deny Posture

As of January 2023, all new S3 buckets are private by default with Block Public Access
enabled at the account level. This was a direct response to years of data breaches caused
by misconfigured buckets.

Key defaults for new buckets:
- **Block Public Access**: All four settings enabled
- **ACLs disabled**: Bucket Owner Enforced is the default object ownership setting
- **Default encryption**: SSE-S3 (AES-256) applied to all new objects
- **No public access**: No bucket policy, no ACL grants to AllUsers or AuthenticatedUsers

### 1.3 Lessons from S3 Data Breaches

Several high-profile breaches (Capital One 2019, US military data exposures, Twitch source
code leak) involved misconfigured S3 buckets. Common root causes:

- Bucket policies with `"Principal": "*"` granting public read/write
- ACLs granting `AllUsers` or `AuthenticatedUsers` access
- Static websites with overly permissive policies
- SSRF attacks escalating to S3 credentials (Capital One)

These incidents drove AWS to implement Block Public Access (2018), S3 Access Analyzer
(2019), and the default-private posture (2023).

### 1.4 Security Is More Than Access Control

S3 security encompasses three pillars:

| Pillar | Concern | S3 Features |
|---|---|---|
| **Confidentiality** | Only authorized parties can read data | IAM, Bucket Policies, Encryption, VPC Endpoints |
| **Integrity** | Data has not been tampered with | SigV4 request signing, Object Lock, Checksums |
| **Auditability** | All access is traceable | CloudTrail, Server Access Logs, S3 Inventory |

---

## 2. Authentication — AWS Signature Version 4 (SigV4)

Every request to S3 must prove the caller's identity. S3 uses AWS Signature Version 4
(SigV4), a cryptographic signing protocol that authenticates requests without ever
transmitting the secret key.

### 2.1 How SigV4 Works — Step by Step

```
Step 1: Create Canonical Request
  ─────────────────────────────
  HTTPMethod + '\n'
  CanonicalURI + '\n'
  CanonicalQueryString + '\n'
  CanonicalHeaders + '\n'
  SignedHeaders + '\n'
  HashedPayload

  Example:
    GET
    /my-bucket/photos/cat.jpg

    host:my-bucket.s3.us-east-1.amazonaws.com
    x-amz-date:20240715T120000Z

    host;x-amz-date
    e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855


Step 2: Create String to Sign
  ────────────────────────────
  "AWS4-HMAC-SHA256" + '\n'
  Timestamp + '\n'
  Scope (date/region/s3/aws4_request) + '\n'
  SHA256(CanonicalRequest)

  Example:
    AWS4-HMAC-SHA256
    20240715T120000Z
    20240715/us-east-1/s3/aws4_request
    7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48d8e28a1d422b8526c


Step 3: Calculate Signing Key (Derived Key)
  ──────────────────────────────────────────
  kDate    = HMAC-SHA256("AWS4" + SecretKey, Date)
  kRegion  = HMAC-SHA256(kDate, Region)
  kService = HMAC-SHA256(kRegion, "s3")
  kSigning = HMAC-SHA256(kService, "aws4_request")

  The signing key is derived fresh for each day/region/service combination.
  This limits the blast radius if a derived key is compromised.


Step 4: Calculate Signature
  ─────────────────────────
  signature = Hex(HMAC-SHA256(kSigning, StringToSign))


Step 5: Add to Request via Authorization Header
  ───────────────────────────────────────────────
  Authorization: AWS4-HMAC-SHA256
    Credential=AKIAIOSFODNN7EXAMPLE/20240715/us-east-1/s3/aws4_request,
    SignedHeaders=host;x-amz-date,
    Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
```

### 2.2 SigV4 Security Properties

**Credential never leaves the client.** The secret access key is used to derive a signing
key, which signs the request. The signature proves possession of the key without revealing it.

**Replay protection.** The timestamp is embedded in the signature. S3 rejects requests where
the timestamp differs from server time by more than 15 minutes.

**Request integrity.** The signed headers and payload hash ensure that the request has not
been modified in transit. If a man-in-the-middle changes the URI, headers, or body, the
signature will not match.

**Scoped credentials.** The signing key is scoped to a specific date, region, and service.
A signing key for `20240715/us-east-1/s3` cannot be used for `us-west-2` or `dynamodb`.

### 2.3 Why SigV4 Over Simpler Auth Mechanisms

| Aspect | SigV4 | Bearer Token (OAuth/JWT) |
|---|---|---|
| Credential exposure | Secret key never sent; only signature | Token sent on every request |
| Replay protection | Timestamp + scope baked into signature | Token valid until expiry; replayable |
| Request integrity | Signed headers + payload detect tampering | No request integrity guarantee |
| Per-request scoping | Scoped to date/region/service | Token has broad, static scope |
| Revocation | Deactivate IAM access key immediately | Token valid until natural expiry (or blacklist) |
| Network sniffing risk | Signature useless without secret key | Stolen token = full access |
| Computational cost | HMAC-SHA256 chain (fast) | RSA/ECDSA verify (heavier) |

### 2.4 SigV4 with Chunked Uploads

For large uploads, S3 supports chunked transfer encoding where each chunk is individually
signed. This allows streaming uploads without buffering the entire payload to compute a
single hash.

```
PUT /my-bucket/large-file HTTP/1.1
Content-Encoding: aws-chunked
x-amz-content-sha256: STREAMING-AWS4-HMAC-SHA256-PAYLOAD
x-amz-decoded-content-length: 66560

Chunk 1: string-to-sign + chunk-signature + chunk-data
Chunk 2: string-to-sign + chunk-signature + chunk-data
Final:   string-to-sign + chunk-signature + (empty)
```

Each chunk's signature chains from the previous chunk, creating a hash chain that detects
any tampering or reordering of chunks.

---

## 3. Authorization — IAM Policies, Bucket Policies, ACLs

Authentication answers "who are you?" Authorization answers "what can you do?" S3 uses
three mechanisms for authorization, each with different scoping and attachment points.

### 3.1 IAM Policies (Identity-Based)

IAM policies are attached to IAM **identities** — users, groups, or roles. They define what
that identity is allowed (or denied) to do across any AWS service.

**Example: Allow user to read objects from a specific bucket**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowGetObjectFromAnalytics",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectVersion"
      ],
      "Resource": "arn:aws:s3:::analytics-data/*"
    }
  ]
}
```

**Example: Allow a role to list and read from multiple prefixes**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowListBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::shared-data-lake",
      "Condition": {
        "StringLike": {
          "s3:prefix": [
            "team-alpha/*",
            "team-beta/*"
          ]
        }
      }
    },
    {
      "Sid": "AllowReadObjects",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": [
        "arn:aws:s3:::shared-data-lake/team-alpha/*",
        "arn:aws:s3:::shared-data-lake/team-beta/*"
      ]
    }
  ]
}
```

**Key characteristics:**
- Travel with the identity — apply regardless of which bucket they access
- Cannot grant cross-account access by themselves (need bucket policy too)
- Maximum policy size: 6,144 characters (inline), 10,240 characters (managed)

### 3.2 Bucket Policies (Resource-Based)

Bucket policies are attached to the **bucket** itself. They define which principals can
perform which actions on the bucket and its objects.

**Example: Allow cross-account access for a specific role**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CrossAccountReadAccess",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:role/DataPipelineRole"
      },
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::my-bucket",
        "arn:aws:s3:::my-bucket/*"
      ]
    }
  ]
}
```

**Example: Restrict uploads to encrypted objects only**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyUnencryptedUploads",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::sensitive-data/*",
      "Condition": {
        "StringNotEquals": {
          "s3:x-amz-server-side-encryption": "aws:kms"
        }
      }
    }
  ]
}
```

**Example: Restrict access to specific VPC endpoint**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RestrictToVPCEndpoint",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::confidential-data",
        "arn:aws:s3:::confidential-data/*"
      ],
      "Condition": {
        "StringNotEquals": {
          "aws:sourceVpce": "vpce-1a2b3c4d"
        }
      }
    }
  ]
}
```

**Key characteristics:**
- Live on the bucket — apply to all principals who access it
- Can grant cross-account access (bucket policy alone is sufficient for same-account)
- Maximum policy size: 20 KB
- Can reference principals from other AWS accounts

### 3.3 ACLs (Access Control Lists) — Legacy

ACLs are the original access control mechanism for S3, predating IAM by several years.
They are a simplified, limited form of access control.

**ACL grant types:**

| Grantee | Description |
|---|---|
| Bucket owner | The AWS account that owns the bucket |
| Specific AWS account | Identified by canonical user ID or email |
| AllUsers | Anyone on the internet (public access) |
| AuthenticatedUsers | Any AWS authenticated user (essentially public) |
| LogDelivery | S3 log delivery group |

**ACL permission types:**

| Permission | Bucket | Object |
|---|---|---|
| READ | List objects | Read object data |
| WRITE | Create/delete objects | N/A |
| READ_ACP | Read bucket ACL | Read object ACL |
| WRITE_ACP | Write bucket ACL | Write object ACL |
| FULL_CONTROL | All of the above | All of the above |

**Why ACLs should be avoided:**

- Limited expressiveness: no conditions, no deny statements, no prefix-level scoping
- Confusing ownership semantics: objects uploaded by other accounts are owned by the uploader
- Security risk: `AllUsers` and `AuthenticatedUsers` grants are the #1 cause of S3 data exposure
- AWS recommendation: Set "Bucket Owner Enforced" (S3 Object Ownership) to disable ACLs entirely

```
S3 Object Ownership Settings:
  ┌────────────────────────────┬──────────────────────────────────┐
  │ Setting                    │ Behavior                         │
  ├────────────────────────────┼──────────────────────────────────┤
  │ Bucket owner enforced      │ ACLs disabled. Bucket owner owns │
  │ (Recommended, default)     │ all objects. Policies only.      │
  ├────────────────────────────┼──────────────────────────────────┤
  │ Bucket owner preferred     │ ACLs enabled. Bucket owner owns  │
  │                            │ objects if bucket-owner-full-    │
  │                            │ control ACL is set on upload.    │
  ├────────────────────────────┼──────────────────────────────────┤
  │ Object writer (legacy)     │ ACLs enabled. Uploading account  │
  │                            │ owns the object.                 │
  └────────────────────────────┴──────────────────────────────────┘
```

### 3.4 Comparison: IAM Policies vs. Bucket Policies vs. ACLs

| Feature | IAM Policy | Bucket Policy | ACL |
|---|---|---|---|
| Attached to | Identity (user/role/group) | Bucket | Bucket or Object |
| Scope | All services | Single bucket | Single bucket/object |
| Cross-account | Requires bucket policy too | Yes (standalone) | Yes (limited) |
| Conditions | Yes (IP, VPC, time, etc.) | Yes (IP, VPC, time, etc.) | No |
| Deny statements | Yes | Yes | No |
| Prefix-level | Yes (via Resource ARN) | Yes (via Resource ARN) | No |
| Max size | 10 KB | 20 KB | 100 grants |
| Recommended | Yes | Yes | No (disable via Object Ownership) |

---

## 4. Policy Evaluation Logic

When a request arrives at S3, the authorization engine evaluates all applicable policies
to determine if the request should be allowed or denied.

### 4.1 Same-Account Policy Evaluation

```
                    ┌──────────────────┐
                    │   S3 Request     │
                    │  (authenticated) │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Gather all      │
                    │  applicable      │
                    │  policies:       │
                    │  - IAM policy    │
                    │  - Bucket policy │
                    │  - ACL (if any)  │
                    │  - SCP (if any)  │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Explicit DENY   │
                    │  in ANY policy?  │──── YES ──► DENY (403)
                    └────────┬─────────┘
                             │ NO
                    ┌────────▼─────────┐
                    │  Explicit ALLOW  │
                    │  in ANY policy?  │──── YES ──► ALLOW (200)
                    └────────┬─────────┘
                             │ NO
                    ┌────────▼─────────┐
                    │  Implicit DENY   │
                    │  (default)       │──────────► DENY (403)
                    └──────────────────┘
```

**The golden rule: Explicit Deny > Explicit Allow > Implicit Deny**

This means:
- A single `"Effect": "Deny"` anywhere overrides all Allow statements
- An Allow statement is needed to permit access (default is deny)
- The union of all policies is evaluated, not just one

### 4.2 Cross-Account Policy Evaluation

Cross-account access is more restrictive. **Both** the requesting account and the
owning account must grant permission.

```
Account A (Requester)              Account B (Bucket Owner)
┌──────────────────────┐          ┌──────────────────────┐
│                      │          │                      │
│  IAM Policy must     │          │  Bucket Policy must  │
│  ALLOW the action    │          │  ALLOW the principal │
│  on the bucket ARN   │          │  from Account A      │
│                      │          │                      │
└──────────┬───────────┘          └──────────┬───────────┘
           │                                  │
           └──────────┬───────────────────────┘
                      │
              ┌───────▼───────┐
              │  BOTH must    │
              │  allow the    │──── Either denies ──► DENY
              │  request      │
              └───────┬───────┘
                      │ Both allow
                      ▼
                    ALLOW
```

**Example — Cross-account access setup:**

Account A (123456789012) wants to read from Account B's (987654321098) bucket.

**Account A — IAM policy on the role:**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::account-b-bucket/*"
    }
  ]
}
```

**Account B — Bucket policy on `account-b-bucket`:**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:role/CrossAccountReaderRole"
      },
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::account-b-bucket/*"
    }
  ]
}
```

If either policy is missing, the request is denied.

### 4.3 Service Control Policies (SCPs)

In AWS Organizations, SCPs set the maximum permissions boundary for accounts within
an organizational unit. SCPs do not grant permissions — they only restrict them.

```
Effective Permissions = IAM Policy  ∩  Bucket Policy  ∩  SCP  ∩  Permissions Boundary
```

If an SCP denies `s3:DeleteObject`, no IAM policy or bucket policy can override it.

---

## 5. S3 Block Public Access

### 5.1 Background

Block Public Access was introduced in November 2018 as a response to the epidemic of
S3 data breaches caused by misconfigured bucket policies and ACLs. It acts as an
account-level or bucket-level safety net.

### 5.2 The Four Settings

```
┌──────────────────────────────────────────────────────────────┐
│                    Block Public Access                        │
├──────────────────────────┬───────────────────────────────────┤
│                          │                                   │
│  ACL-Based Controls      │  Policy-Based Controls            │
│                          │                                   │
│  1. BlockPublicAcls      │  3. BlockPublicPolicy             │
│     Blocks PUT calls     │     Blocks PUT bucket policy      │
│     that set public      │     calls that grant public       │
│     ACL grants           │     access                        │
│                          │                                   │
│  2. IgnorePublicAcls     │  4. RestrictPublicBuckets         │
│     S3 ignores any       │     Limits access to buckets      │
│     existing public      │     with public policies to       │
│     ACL grants           │     only AWS service principals   │
│                          │     and authorized users          │
└──────────────────────────┴───────────────────────────────────┘
```

**Detailed breakdown:**

| Setting | What It Blocks | When to Use |
|---|---|---|
| `BlockPublicAcls` | Rejects PUT bucket/object ACL if it grants public access | Always (prevents new public ACLs) |
| `IgnorePublicAcls` | Ignores existing ACLs that grant public access | Always (neutralizes legacy public ACLs) |
| `BlockPublicPolicy` | Rejects PUT bucket policy if it grants public access | Always except for public website buckets |
| `RestrictPublicBuckets` | Limits access to bucket with public policies to authorized principals only | Always except for public website buckets |

### 5.3 Account-Level vs. Bucket-Level

Block Public Access can be set at two levels:
- **Account level**: Applies to ALL buckets in the account. Overrides bucket-level settings.
- **Bucket level**: Applies to a single bucket. Cannot be more permissive than account level.

```
Account Level: BlockPublicAcls = true
                                      → Effective: BlockPublicAcls = true
Bucket Level:  BlockPublicAcls = false    (account level wins)

Account Level: BlockPublicAcls = false
                                      → Effective: BlockPublicAcls = true
Bucket Level:  BlockPublicAcls = true     (bucket level can be MORE restrictive)
```

### 5.4 Best Practice

Enable all four settings at the account level. For the rare bucket that genuinely needs
public access (e.g., static website hosting), override at the bucket level by disabling
only the specific settings needed, and document the business justification.

---

## 6. Encryption at Rest

S3 supports three server-side encryption mechanisms and client-side encryption.

### 6.1 SSE-S3 (Server-Side Encryption with S3-Managed Keys)

- S3 fully manages key generation, rotation, and storage
- Uses AES-256 (Advanced Encryption Standard with 256-bit keys)
- Each object is encrypted with a unique data key
- The data key is itself encrypted by a root key that S3 rotates
- No additional cost, no configuration required
- Default encryption for all new objects (as of January 2023)

```
SSE-S3 Encryption Flow:

  Client                           S3                          S3 Key Store
    │                              │                              │
    │── PutObject(data) ──────────►│                              │
    │                              │── Generate data key ────────►│
    │                              │◄── data_key ─────────────────│
    │                              │                              │
    │                              │  encrypt(data, data_key)     │
    │                              │  encrypt(data_key, root_key) │
    │                              │                              │
    │                              │  Store: encrypted_data       │
    │                              │       + encrypted_data_key   │
    │                              │                              │
    │◄── 200 OK ──────────────────│                              │
    │   (x-amz-server-side-       │                              │
    │    encryption: AES256)      │                              │
```

### 6.2 SSE-KMS (Server-Side Encryption with KMS-Managed Keys)

AWS Key Management Service (KMS) manages the encryption key. This provides significantly
more control, auditability, and flexibility compared to SSE-S3.

**Envelope Encryption — The Core Concept:**

```
Envelope Encryption Flow:

  S3                                    KMS
   │                                     │
   │── GenerateDataKey(KeyId=my-cmk) ──►│
   │                                     │
   │◄── { plaintext_data_key,           │
   │      encrypted_data_key } ──────────│
   │                                     │
   │  1. Encrypt object with             │
   │     plaintext_data_key (AES-256)    │
   │                                     │
   │  2. Store: encrypted_data           │
   │         + encrypted_data_key        │
   │         (metadata)                  │
   │                                     │
   │  3. DISCARD plaintext_data_key      │
   │     (NEVER stored on disk)          │
   │                                     │
   │                                     │
   Decryption:                           │
   │                                     │
   │  1. Read encrypted_data_key         │
   │     from object metadata            │
   │                                     │
   │── Decrypt(encrypted_data_key) ─────►│
   │                                     │
   │◄── plaintext_data_key ──────────────│
   │                                     │
   │  2. Decrypt object with             │
   │     plaintext_data_key              │
   │                                     │
   │  3. DISCARD plaintext_data_key      │
```

**Why envelope encryption instead of encrypting directly with the KMS key?**

- KMS has a 4 KB limit on data it can encrypt directly
- KMS API calls add latency; encrypting the data locally with a data key is faster
- The data key is unique per object, limiting the blast radius of key compromise
- The KMS key (CMK) never leaves KMS hardware — it only encrypts/decrypts data keys

**Benefits of SSE-KMS over SSE-S3:**

| Benefit | Detail |
|---|---|
| Audit trail | Every `GenerateDataKey` and `Decrypt` call is logged in CloudTrail |
| Key policy | Separate IAM-like policy on the KMS key controlling who can use it |
| Key rotation | Automatic annual rotation (KMS keeps old versions for decryption) |
| Cross-account | Encrypt with a key from Account A, grant Account B decrypt permissions |
| Customer-managed keys | Create your own KMS key with custom alias, policy, and rotation schedule |
| Grants | Temporary, delegated access to use a key without modifying the key policy |

**Example: Bucket policy requiring SSE-KMS encryption with a specific key**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RequireKMSEncryption",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::regulated-data/*",
      "Condition": {
        "StringNotEquals": {
          "s3:x-amz-server-side-encryption": "aws:kms"
        }
      }
    },
    {
      "Sid": "RequireSpecificKMSKey",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::regulated-data/*",
      "Condition": {
        "StringNotEquals": {
          "s3:x-amz-server-side-encryption-aws-kms-key-id": "arn:aws:kms:us-east-1:111122223333:key/abcd1234-5678-90ef-ghij-klmnopqrstuv"
        }
      }
    }
  ]
}
```

**KMS API cost consideration:**
- Each `PutObject` calls `GenerateDataKey` (1 KMS API call)
- Each `GetObject` calls `Decrypt` (1 KMS API call)
- Cost: $0.03 per 10,000 requests
- S3 Bucket Keys (introduced 2020) reduce costs by caching a bucket-level key, reducing
  KMS calls by up to 99%

```
Without S3 Bucket Keys:              With S3 Bucket Keys:

  PUT obj1 → KMS GenerateDataKey     PUT obj1 → KMS GenerateDataKey (bucket key)
  PUT obj2 → KMS GenerateDataKey     PUT obj2 → Use cached bucket key (no KMS call)
  PUT obj3 → KMS GenerateDataKey     PUT obj3 → Use cached bucket key (no KMS call)
  ...                                ...
  (N KMS calls for N objects)        (1 KMS call per bucket key rotation period)
```

### 6.3 SSE-C (Server-Side Encryption with Customer-Provided Keys)

The customer provides the encryption key with every request. S3 uses the key to
encrypt/decrypt but never stores it.

```
PUT Request:
  Headers:
    x-amz-server-side-encryption-customer-algorithm: AES256
    x-amz-server-side-encryption-customer-key: <base64-encoded-key>
    x-amz-server-side-encryption-customer-key-MD5: <base64-MD5-of-key>

  S3 Action:
    1. Validate key MD5 matches
    2. Encrypt object with provided key
    3. Store encrypted object + key MD5 (NOT the key)
    4. Discard the key from memory
    5. Return 200 OK

GET Request:
  Headers:
    x-amz-server-side-encryption-customer-algorithm: AES256
    x-amz-server-side-encryption-customer-key: <same-base64-encoded-key>
    x-amz-server-side-encryption-customer-key-MD5: <base64-MD5-of-key>

  S3 Action:
    1. Validate key MD5 matches stored MD5
    2. Decrypt object with provided key
    3. Return decrypted data
    4. Discard key from memory
```

**Critical: If you lose the key, the data is permanently inaccessible.** S3 does not have
a copy of the key and cannot recover it.

**SSE-C requirements:**
- HTTPS is mandatory (S3 rejects HTTP requests with SSE-C headers)
- Key must be a 256-bit AES key
- Key MD5 must be provided for integrity checking

### 6.4 Client-Side Encryption

The customer encrypts data before sending it to S3. S3 stores the ciphertext and has
no knowledge of the encryption key or process. This provides the strongest guarantee
that S3 (and AWS) cannot read the data, but places the full burden of key management
on the customer.

### 6.5 Encryption Comparison Table

| Feature | SSE-S3 | SSE-KMS | SSE-C | Client-Side |
|---|---|---|---|---|
| Key management | S3 | KMS | Customer | Customer |
| Key storage | S3 internal | KMS HSM | Customer's system | Customer's system |
| Audit trail | No | Yes (CloudTrail) | No | No |
| Key rotation | Auto (S3 internal) | Auto (annual) | Customer manages | Customer manages |
| Cross-account key | No | Yes | N/A | N/A |
| Cost | Free | KMS API fees | Free | Free |
| Risk of key loss | None | Low | High | High |
| S3 features compat | Full | Full | Limited (no S3 Batch) | Limited |
| Regulatory strength | Basic | Strong | Strongest (server-side) | Strongest (overall) |
| Configuration | None (default) | Specify KMS key ID | Key in every request | App-level |

---

## 7. Encryption in Transit

### 7.1 TLS Enforcement

All S3 endpoints support HTTPS (TLS 1.2+). However, S3 also accepts HTTP by default.
To enforce HTTPS, apply a bucket policy that denies HTTP requests.

**Bucket policy to enforce HTTPS:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyHTTP",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::my-bucket",
        "arn:aws:s3:::my-bucket/*"
      ],
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    }
  ]
}
```

### 7.2 TLS Version Requirements

- S3 supports TLS 1.2 and TLS 1.3
- TLS 1.0 and 1.1 are deprecated
- For compliance, enforce minimum TLS version via bucket policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EnforceTLS12",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": "arn:aws:s3:::my-bucket/*",
      "Condition": {
        "NumericLessThan": {
          "s3:TlsVersion": "1.2"
        }
      }
    }
  ]
}
```

### 7.3 End-to-End Encryption

For maximum security, combine encryption in transit (HTTPS) with encryption at rest
(SSE-KMS) and client-side encryption:

```
Client ──TLS──► S3 Endpoint ──► SSE-KMS Encryption ──► Encrypted Storage
  │                                                           │
  │  (data encrypted by client before sending)                │
  │  (TLS protects in transit)                                │
  │  (SSE-KMS encrypts at rest)                               │
  │                                                           │
  └──── Three layers of encryption ───────────────────────────┘
```

---

## 8. S3 Object Lock — WORM Compliance

### 8.1 Purpose

Object Lock provides Write Once Read Many (WORM) protection. Once an object is locked,
it cannot be deleted or overwritten for the duration of the retention period. This is
required by regulations including:

- **SEC Rule 17a-4(f)**: Electronic storage of broker-dealer records
- **FINRA Rule 4511**: Books and records retention
- **HIPAA**: Healthcare data retention requirements
- **GDPR**: Right to erasure vs. legal hold conflicts
- **CFTC Rule 1.31**: Commodity trading records

### 8.2 Prerequisites

- Object Lock can only be enabled when creating a new bucket
- Versioning is automatically enabled (and cannot be suspended)
- Object Lock applies to specific object versions, not the key name

### 8.3 Retention Modes

**Governance Mode:**

```
┌─────────────────────────────────────────────────────────────┐
│  GOVERNANCE MODE                                            │
│                                                             │
│  Retention: Object CANNOT be deleted/overwritten            │
│                                                             │
│  Override: Users with s3:BypassGovernanceRetention AND      │
│            x-amz-bypass-governance-retention:true header    │
│            CAN delete/overwrite                             │
│                                                             │
│  Use case: Internal compliance policies where               │
│            administrators need emergency override            │
│                                                             │
│  Example: Retain financial reports for 7 years,             │
│           but CFO can override if report is incorrect       │
└─────────────────────────────────────────────────────────────┘
```

**Compliance Mode:**

```
┌─────────────────────────────────────────────────────────────┐
│  COMPLIANCE MODE                                            │
│                                                             │
│  Retention: Object CANNOT be deleted/overwritten            │
│                                                             │
│  Override: IMPOSSIBLE. Not even the root account.           │
│            Not even AWS support. NO ONE.                    │
│                                                             │
│  The retention period CANNOT be shortened once set.         │
│  It CAN be extended.                                        │
│                                                             │
│  Use case: Regulatory requirements where immutability       │
│            must be provable and unbreakable                 │
│                                                             │
│  Example: SEC Rule 17a-4 broker-dealer records              │
└─────────────────────────────────────────────────────────────┘
```

### 8.4 Legal Hold

Legal Hold is separate from retention periods. It provides an indefinite lock on an
object version.

| Aspect | Retention Period | Legal Hold |
|---|---|---|
| Duration | Fixed (date or days) | Indefinite |
| Removal | Automatic when period expires | Manual (requires `s3:PutObjectLegalHold`) |
| Can coexist | Yes | Yes |
| Root override (Compliance) | No | N/A (it's a separate toggle) |
| Use case | Regulatory data retention | Litigation holds, investigations |

**Example: Setting Object Lock configuration on a bucket**

```json
{
  "ObjectLockConfiguration": {
    "ObjectLockEnabled": "Enabled",
    "Rule": {
      "DefaultRetention": {
        "Mode": "COMPLIANCE",
        "Days": 2555
      }
    }
  }
}
```

---

## 9. MFA Delete

### 9.1 What It Protects

MFA Delete requires multi-factor authentication for two destructive operations:

1. **Changing the versioning state** of a bucket (disabling or suspending versioning)
2. **Permanently deleting** an object version

Regular delete operations (which create a delete marker) are NOT affected. Only the
permanent deletion of a specific version ID requires MFA.

### 9.2 Enabling MFA Delete

- Can ONLY be enabled by the bucket owner (root account credentials)
- Cannot be enabled via the AWS Console — must use CLI or API
- Requires the root account's MFA device serial number and code

```
aws s3api put-bucket-versioning \
  --bucket my-critical-bucket \
  --versioning-configuration Status=Enabled,MFADelete=Enabled \
  --mfa "arn:aws:iam::123456789012:mfa/root-mfa-device 123456"
```

### 9.3 Protection Scenario

```
Without MFA Delete:                  With MFA Delete:

  Attacker gets admin creds          Attacker gets admin creds
           │                                  │
           ▼                                  ▼
  Disable versioning ✓               Disable versioning ✗
  Delete all versions ✓              (requires MFA code)
  Data permanently lost              Delete version ✗
                                     (requires MFA code)
                                     Data is SAFE
```

---

## 10. S3 Access Points

### 10.1 The Problem

A single bucket policy managing access for dozens of applications becomes:
- Complex (thousands of lines of JSON)
- Fragile (one bad edit affects all applications)
- Size-limited (20 KB max for bucket policies)
- Hard to audit (who has access to what?)

### 10.2 The Solution: Named Network Endpoints

Each Access Point is a named network endpoint with its own:
- Access policy (up to 20 KB each)
- Network origin control (Internet or VPC-only)
- DNS name

```
                     ┌─────────────────────────────────────────┐
                     │         Bucket: shared-data-lake         │
                     │                                         │
                     │  /analytics/...                         │
                     │  /training-data/...                     │
                     │  /public-reports/...                    │
                     └───────┬──────────┬──────────┬───────────┘
                             │          │          │
                     ┌───────┘          │          └───────┐
                     │                  │                  │
              ┌──────▼──────┐   ┌──────▼──────┐   ┌──────▼──────┐
              │  Access     │   │  Access     │   │  Access     │
              │  Point:     │   │  Point:     │   │  Point:     │
              │  analytics  │   │  ml-team    │   │  public     │
              │             │   │             │   │             │
              │  VPC-only   │   │  VPC-only   │   │  Internet   │
              │  prefix:    │   │  prefix:    │   │  prefix:    │
              │  analytics/ │   │  training-  │   │  public-    │
              │             │   │  data/      │   │  reports/   │
              └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
                     │                 │                  │
              ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
              │  Analytics  │  │  ML Training│  │  Public     │
              │  Team       │  │  Pipeline   │  │  Website    │
              └─────────────┘  └─────────────┘  └─────────────┘
```

### 10.3 Access Point Policy Example

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:role/AnalyticsTeamRole"
      },
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:us-east-1:123456789012:accesspoint/analytics-team",
        "arn:aws:s3:us-east-1:123456789012:accesspoint/analytics-team/object/analytics/*"
      ]
    }
  ]
}
```

### 10.4 Delegating Access Control to Access Points

The bucket policy can delegate ALL access control to access points:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::shared-data-lake",
        "arn:aws:s3:::shared-data-lake/*"
      ],
      "Condition": {
        "StringEquals": {
          "s3:DataAccessPointAccount": "123456789012"
        }
      }
    }
  ]
}
```

This policy says: "Allow any action, as long as it comes through an access point owned
by account 123456789012." The individual access point policies then control who can do what.

---

## 11. VPC Endpoints for S3

### 11.1 Gateway Endpoint (Free)

Gateway endpoints route S3 traffic through the AWS private network, eliminating the
need for an Internet Gateway, NAT Gateway, or public IP addresses.

```
Without Gateway Endpoint:           With Gateway Endpoint:

  EC2 Instance                        EC2 Instance
       │                                   │
       ▼                                   ▼
  NAT Gateway ($)                     Route Table
       │                              (prefix list for S3)
       ▼                                   │
  Internet Gateway                         ▼
       │                              Gateway VPC Endpoint
       ▼                                   │
  Public Internet                          ▼
       │                              AWS Private Network
       ▼                                   │
  S3 Endpoint                              ▼
                                      S3 Endpoint

  Cost: NAT Gateway hourly +          Cost: FREE
        data processing fees
  Security: Traffic traverses         Security: Traffic stays
            public internet                     within AWS
```

**Gateway endpoint policy example:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": [
        "s3:GetObject",
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::my-private-bucket/*"
    }
  ]
}
```

### 11.2 Interface Endpoint (PrivateLink)

Interface endpoints create Elastic Network Interfaces (ENIs) in your VPC subnets,
providing private DNS resolution to S3.

| Feature | Gateway Endpoint | Interface Endpoint |
|---|---|---|
| Cost | Free | Hourly charge + data processing |
| Network path | Route table entry | ENI in subnet |
| On-premises access | No (VPC only) | Yes (via VPN/Direct Connect) |
| Cross-region | No | Yes |
| DNS | Public S3 DNS resolves to gateway | Private DNS resolves to ENI IPs |
| Security groups | No | Yes (SG on ENI) |

### 11.3 Combining VPC Endpoints with Bucket Policies

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowVPCEndpointOnly",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::internal-data",
        "arn:aws:s3:::internal-data/*"
      ],
      "Condition": {
        "StringNotEquals": {
          "aws:sourceVpce": [
            "vpce-1a2b3c4d",
            "vpce-5e6f7g8h"
          ]
        }
      }
    }
  ]
}
```

This ensures data can ONLY be accessed through specific VPC endpoints — not from the
public internet, AWS Console, or any other network path.

---

## 12. Presigned URLs — Temporary Access

### 12.1 Concept

Presigned URLs grant time-limited access to a specific S3 object without requiring the
recipient to have AWS credentials. The URL embeds the signer's credentials (as a signature)
and an expiration time.

### 12.2 Generation

```python
# Server-side: Generate presigned URL using AWS SDK (Python/boto3)
import boto3

s3_client = boto3.client('s3', region_name='us-east-1')

# Presigned GET (download)
download_url = s3_client.generate_presigned_url(
    'get_object',
    Params={
        'Bucket': 'my-bucket',
        'Key': 'private/report.pdf'
    },
    ExpiresIn=3600  # 1 hour
)

# Presigned PUT (upload)
upload_url = s3_client.generate_presigned_url(
    'put_object',
    Params={
        'Bucket': 'my-bucket',
        'Key': 'uploads/user-photo.jpg',
        'ContentType': 'image/jpeg'
    },
    ExpiresIn=900  # 15 minutes
)
```

### 12.3 URL Structure

```
https://my-bucket.s3.amazonaws.com/private/report.pdf
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=AKIAIOSFODNN7EXAMPLE/20240715/us-east-1/s3/aws4_request
  &X-Amz-Date=20240715T100000Z
  &X-Amz-Expires=3600
  &X-Amz-SignedHeaders=host
  &X-Amz-Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
```

The URL contains:
- The object path
- The credential scope (access key ID, date, region, service)
- The timestamp when the URL was generated
- The expiration time in seconds
- The signature (HMAC-SHA256 of the canonical request)

### 12.4 Maximum Expiry Times

The maximum lifetime of a presigned URL depends on the credential type used to sign it:

| Credential Type | Max Expiry | Notes |
|---|---|---|
| IAM user (long-lived access key) | 7 days (604,800 seconds) | Not recommended; use roles |
| STS temporary credentials (AssumeRole) | 12 hours (43,200 seconds) | Session duration limit |
| IAM role (instance profile) | 6 hours (21,600 seconds) | Refreshed automatically by SDK |
| AWS SSO credentials | 12 hours | Session duration |

### 12.5 Security Considerations

- **The URL is the credential**: Anyone with the URL can access the object until it expires
- **Revocation**: You cannot revoke a presigned URL directly. You can:
  - Delete the object
  - Deactivate the IAM user's access key (if signed with user credentials)
  - Add an explicit deny in bucket policy for the signer's credentials
- **Audit**: Presigned URL access appears in CloudTrail as the signer's identity, not the
  URL recipient
- **HTTPS**: Always generate presigned URLs using HTTPS endpoints

### 12.6 Presigned POST (Browser Uploads)

For browser-based uploads, use presigned POST policies which support conditions:

```json
{
  "expiration": "2024-07-15T12:00:00.000Z",
  "conditions": [
    {"bucket": "user-uploads"},
    ["starts-with", "$key", "uploads/"],
    {"acl": "private"},
    ["content-length-range", 1, 10485760],
    {"x-amz-server-side-encryption": "aws:kms"}
  ]
}
```

This allows uploads only to the `uploads/` prefix, with a maximum file size of 10 MB,
private ACL, and KMS encryption required.

---

## 13. Logging & Audit

### 13.1 S3 Server Access Logs

Server access logging provides detailed records of every request made to an S3 bucket.

**Configuration:**

```xml
<BucketLoggingStatus>
  <LoggingEnabled>
    <TargetBucket>my-access-logs-bucket</TargetBucket>
    <TargetPrefix>logs/my-bucket/</TargetPrefix>
  </LoggingEnabled>
</BucketLoggingStatus>
```

**Log record fields:**

```
Bucket Owner | Bucket | Time | Remote IP | Requester | Request ID |
Operation | Key | Request-URI | HTTP Status | Error Code |
Bytes Sent | Object Size | Total Time | Turn-Around Time |
Referrer | User-Agent | Version ID | Host ID |
Signature Version | Cipher Suite | Authentication Type |
Host Header | TLS Version | Access Point ARN
```

**Example log entry:**

```
79a59df900b949e55d96a1e698fbacedfd6e09d98eacf8f8d5218e7cd47ef2be
  my-bucket [06/Feb/2024:00:00:50 +0000] 192.0.2.3
  arn:aws:iam::123456789012:user/alice AIDEXAMPLE
  REST.GET.OBJECT photos/cat.jpg
  "GET /photos/cat.jpg HTTP/1.1" 200 -
  2048 2048 42 41
  "-" "aws-sdk-java/2.20.0" -
  AIDEXAMPLE SigV4 ECDHE-RSA-AES128-GCM-SHA256 AuthHeader
  my-bucket.s3.us-east-1.amazonaws.com TLSv1.2 -
```

**Limitations:**
- Best-effort delivery (not guaranteed for every request)
- Typically delivered within a few hours
- Logs may contain duplicate records
- Not suitable for real-time alerting (use CloudTrail for that)

### 13.2 AWS CloudTrail

CloudTrail provides a complete, guaranteed audit trail of S3 API calls.

**Management events (enabled by default):**
- `CreateBucket`, `DeleteBucket`
- `PutBucketPolicy`, `PutBucketAcl`
- `PutBucketEncryption`, `PutBucketVersioning`

**Data events (optional, extra cost):**
- `GetObject`, `PutObject`, `DeleteObject`
- `HeadObject`, `GetObjectAcl`

**CloudTrail log example (PutObject):**

```json
{
  "eventVersion": "1.08",
  "eventTime": "2024-07-15T12:34:56Z",
  "eventSource": "s3.amazonaws.com",
  "eventName": "PutObject",
  "awsRegion": "us-east-1",
  "sourceIPAddress": "192.0.2.1",
  "userAgent": "aws-cli/2.15.0",
  "requestParameters": {
    "bucketName": "my-bucket",
    "key": "data/report.csv",
    "x-amz-server-side-encryption": "aws:kms",
    "x-amz-server-side-encryption-aws-kms-key-id": "arn:aws:kms:us-east-1:123456789012:key/abcd1234"
  },
  "responseElements": {
    "x-amz-server-side-encryption": "aws:kms",
    "x-amz-version-id": "3sL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY+MTRCxf3vjVBH40Nr8X8gdRQBpUMLUo"
  },
  "userIdentity": {
    "type": "AssumedRole",
    "arn": "arn:aws:sts::123456789012:assumed-role/DataPipelineRole/session1",
    "accountId": "123456789012"
  }
}
```

### 13.3 Amazon S3 Inventory

S3 Inventory provides a scheduled report of objects and their metadata. Useful for
compliance audits — verifying encryption status, replication status, and storage class
for all objects.

**Output fields:**
- Bucket, Key, Version ID
- Object size, Last modified date
- Encryption status (SSE-S3, SSE-KMS, SSE-C, none)
- Replication status
- Object Lock retention mode and date
- Storage class
- Checksum algorithm

### 13.4 Amazon Macie

Amazon Macie is a data security service that uses machine learning to discover and
protect sensitive data in S3:

- Scans bucket contents for PII (credit card numbers, SSNs, passport numbers)
- Identifies sensitive data patterns
- Alerts on public or unencrypted buckets
- Integrates with Security Hub for centralized findings

### 13.5 S3 Access Analyzer (IAM Access Analyzer)

Analyzes bucket policies and access points to identify resources that are accessible
from outside your account or organization:

```
S3 Access Analyzer Findings:

  Finding 1: Bucket "legacy-data" is publicly accessible
    - Policy grants access to Principal: "*"
    - No VPC condition
    - Recommendation: Add Block Public Access

  Finding 2: Bucket "shared-reports" grants cross-account access
    - Policy grants access to account 987654321098
    - Actions: s3:GetObject, s3:ListBucket
    - Recommendation: Verify intended cross-account access
```

---

## 14. Common Security Anti-Patterns & Mitigations

### 14.1 Summary Table

| # | Anti-Pattern | Risk | Mitigation |
|---|---|---|---|
| 1 | Public bucket policy (`Principal: "*"`) | Data exposure to the internet | Block Public Access (all four settings) |
| 2 | Wildcard actions (`s3:*`) in policies | Excessive permissions (least privilege violation) | Specific actions only (`s3:GetObject`) |
| 3 | No encryption | Data at rest readable if storage compromised | Default SSE-S3; SSE-KMS for sensitive data |
| 4 | HTTP allowed | Man-in-the-middle attacks | Enforce HTTPS via `aws:SecureTransport` condition |
| 5 | No versioning | Ransomware can overwrite; accidental delete permanent | Enable versioning + MFA Delete |
| 6 | Long-lived IAM access keys | Compromised keys provide indefinite access | IAM roles with STS temporary credentials |
| 7 | Single bucket for all data | Large blast radius if compromised | Separate buckets by data classification |
| 8 | No access logging | Blind to breaches, no forensic trail | CloudTrail data events + Server Access Logs |
| 9 | ACLs granting `AllUsers` | Equivalent to public access | Disable ACLs (Bucket Owner Enforced) |
| 10 | No IP/VPC restrictions | Accessible from anywhere | VPC endpoint conditions in bucket policies |

### 14.2 Detailed Anti-Pattern: Overly Permissive Bucket Policy

**Bad:**
```json
{
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": "arn:aws:s3:::my-bucket/*"
  }]
}
```

This grants EVERYONE (including unauthenticated users) FULL ACCESS to ALL objects.

**Fixed:**
```json
{
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "AWS": "arn:aws:iam::123456789012:role/AppServerRole"
    },
    "Action": [
      "s3:GetObject",
      "s3:PutObject"
    ],
    "Resource": "arn:aws:s3:::my-bucket/app-data/*",
    "Condition": {
      "StringEquals": {
        "aws:sourceVpce": "vpce-1a2b3c4d"
      }
    }
  }]
}
```

This grants a specific role, specific actions, on a specific prefix, from a specific
VPC endpoint.

### 14.3 Detailed Anti-Pattern: No Versioning + No MFA Delete

**Attack scenario (ransomware):**

```
1. Attacker gains write access (compromised credentials)
2. Attacker overwrites all objects with encrypted (ransomed) versions
3. Without versioning: original data is GONE
4. With versioning but no MFA Delete: attacker deletes all versions
5. With versioning + MFA Delete: attacker cannot delete versions
   (requires MFA code from physical device)
```

### 14.4 Detailed Anti-Pattern: Using IAM Users Instead of Roles

**Bad:** Application on EC2 uses an IAM user's access keys hardcoded in configuration.

```
# .env file on EC2 instance
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

**Risks:**
- Keys don't expire (valid until manually rotated)
- Keys can be committed to source control
- Keys can be extracted via SSRF attacks
- Keys are the same for every request (no request-level scoping)

**Fixed:** Use an IAM role attached to the EC2 instance (instance profile). The SDK
automatically retrieves temporary credentials from the instance metadata service.

```
# No hardcoded credentials needed
# SDK automatically uses instance profile
import boto3
s3 = boto3.client('s3')  # Credentials from instance metadata
```

Temporary credentials:
- Expire after 1-12 hours
- Automatically rotated by the SDK
- Cannot be committed to source control (they change constantly)
- Scoped to the role's permissions

---

## 15. Cross-Account Access Patterns

### 15.1 Pattern 1: Bucket Policy Grants Access to Another Account's Role

This is the most common pattern. The bucket owner creates a bucket policy that allows
a role from another account to access the bucket.

```
Account A (111111111111)              Account B (222222222222)
Bucket Owner                          Data Consumer

┌──────────────────────┐             ┌──────────────────────┐
│                      │             │                      │
│  Bucket: data-share  │             │  Role: ConsumerRole  │
│                      │             │                      │
│  Bucket Policy:      │             │  IAM Policy:         │
│  Allow ConsumerRole  │◄────────────│  Allow s3:GetObject  │
│  from Account B to   │   request   │  on data-share/*     │
│  s3:GetObject        │             │                      │
│                      │             │                      │
└──────────────────────┘             └──────────────────────┘
```

**Account A — Bucket policy:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CrossAccountRead",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::222222222222:role/ConsumerRole"
      },
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::data-share/*"
    }
  ]
}
```

**Account B — IAM policy on ConsumerRole:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::data-share/*"
    }
  ]
}
```

**Both policies are required.** If either is missing, the request is denied.

### 15.2 Pattern 2: IAM Role with AssumeRole Trust Policy

In this pattern, Account A creates an IAM role in its own account that Account B's
principal can assume. This gives Account B's principal temporary credentials that
operate within Account A.

```
Account A (111111111111)              Account B (222222222222)
Bucket Owner                          Data Consumer

┌──────────────────────┐             ┌──────────────────────┐
│                      │             │                      │
│  Role: SharedAccess  │             │  Role: ConsumerRole  │
│  Trust Policy:       │             │                      │
│    Allow ConsumerRole│◄────STS─────│  1. AssumeRole       │
│    from Account B    │  AssumeRole │     (SharedAccess    │
│    to assume this    │             │      in Account A)   │
│    role              │             │                      │
│                      │             │  2. Use temp creds   │
│  IAM Policy:         │             │     to access S3     │
│    Allow s3:Get on   │             │                      │
│    data-share/*      │             │                      │
│                      │             │                      │
└──────────────────────┘             └──────────────────────┘
```

**Account A — Trust policy on SharedAccess role:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::222222222222:role/ConsumerRole"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "unique-external-id-12345"
        }
      }
    }
  ]
}
```

**Advantage:** Objects accessed this way are owned by Account A (the bucket owner),
avoiding the object ownership problem that occurs with cross-account PutObject using
bucket policies and ACLs.

### 15.3 Pattern 3: S3 Access Points with Cross-Account Permissions

Access Points can grant cross-account access without modifying the bucket policy.

**Account A — Access Point policy:**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::222222222222:root"
      },
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:us-east-1:111111111111:accesspoint/partner-access",
        "arn:aws:s3:us-east-1:111111111111:accesspoint/partner-access/object/*"
      ]
    }
  ]
}
```

**Account A — Bucket policy (delegates to access points):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::shared-data",
        "arn:aws:s3:::shared-data/*"
      ],
      "Condition": {
        "StringEquals": {
          "s3:DataAccessPointAccount": "111111111111"
        }
      }
    }
  ]
}
```

### 15.4 Cross-Account Access Decision Flow

```
                ┌─────────────────────────────────────────┐
                │       Cross-Account S3 Request          │
                │  (Account B → Account A's bucket)       │
                └──────────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │ Check Account A's SCPs      │
                    │ (if in AWS Organization)     │
                    │ Explicit Deny?               │──── YES ──► DENY
                    └──────────────┬──────────────┘
                                   │ NO
                    ┌──────────────▼──────────────┐
                    │ Check Account B's SCPs      │
                    │ (if in AWS Organization)     │
                    │ Explicit Deny?               │──── YES ──► DENY
                    └──────────────┬──────────────┘
                                   │ NO
                    ┌──────────────▼──────────────┐
                    │ Account B: IAM Policy       │
                    │ allows the action?           │──── NO ───► DENY
                    └──────────────┬──────────────┘
                                   │ YES
                    ┌──────────────▼──────────────┐
                    │ Account A: Bucket Policy    │
                    │ or Access Point Policy      │
                    │ allows the principal?        │──── NO ───► DENY
                    └──────────────┬──────────────┘
                                   │ YES
                    ┌──────────────▼──────────────┐
                    │ Any Explicit Deny in        │
                    │ any evaluated policy?        │──── YES ──► DENY
                    └──────────────┬──────────────┘
                                   │ NO
                                   ▼
                                 ALLOW
```

---

## 16. Interview Tips — Security Questions

### 16.1 Common Interview Questions and Key Talking Points

**Q: "How would you secure an S3 bucket containing PII?"**

Answer framework:
1. **Block Public Access** — all four settings at account level
2. **Bucket policy** — restrict to specific VPC endpoint and roles
3. **SSE-KMS encryption** — with customer-managed key for audit trail
4. **Enforce HTTPS** — `aws:SecureTransport` condition
5. **Versioning + MFA Delete** — protect against accidental/malicious deletion
6. **CloudTrail data events** — full audit trail of every access
7. **Macie** — automated PII detection and alerting
8. **S3 Object Lock (Compliance mode)** — if regulatory retention is required

**Q: "Explain the difference between SSE-S3, SSE-KMS, and SSE-C."**

Focus on: key management responsibility, audit trail (CloudTrail for KMS), envelope
encryption, cost implications, and regulatory requirements.

**Q: "A customer reports their S3 bucket is publicly accessible. How do you fix it?"**

1. Immediately enable Block Public Access (all four settings)
2. Review and remove any `Principal: "*"` statements from bucket policy
3. Disable ACLs (Bucket Owner Enforced)
4. Check CloudTrail for recent access patterns to assess data exposure
5. Enable S3 Access Analyzer to find any remaining public access paths
6. Review S3 Inventory for encryption status of all objects

**Q: "How does cross-account S3 access work?"**

Key points: both accounts must explicitly allow (IAM policy in requester account + bucket
policy in owning account). Mention the object ownership problem (uploading account owns
the object by default with ACLs) and the AssumeRole pattern as the cleanest solution.

### 16.2 Security Depth Signals for Interviewers

To demonstrate deep understanding, mention:

- **S3 Bucket Keys** for reducing KMS costs at scale
- **VPC endpoints + bucket policy conditions** together (network + identity + resource layers)
- **STS temporary credentials** over long-lived access keys
- **SCPs as permission boundaries** in multi-account organizations
- **Object Lock Compliance mode** vs Governance mode for regulatory requirements
- **Envelope encryption** — explain WHY (4 KB KMS limit, performance, blast radius)
- **SigV4 signing** — understand the security properties (no credential exposure, replay
  protection, request integrity)

---

## Footer — Cross-References

This document is part of a comprehensive Amazon S3 system design series:

| Document | Focus |
|---|---|
| [Interview Simulation](interview-simulation.md) | Full 45-minute mock interview walkthrough |
| [Metadata & Indexing](metadata-and-indexing.md) | How S3 indexes and retrieves objects at scale |
| [Data Storage & Durability](data-storage-and-durability.md) | 11 nines durability, erasure coding, replication |
| [Consistency & Replication](consistency-and-replication.md) | Strong consistency model, CRR/SRR |
| [Storage Classes & Lifecycle](storage-classes-and-lifecycle.md) | S3 Standard through Glacier Deep Archive |
| [System Flows](flow.md) | PUT/GET/DELETE request flows through the system |
| [Scaling & Performance](scaling-and-performance.md) | Partitioning, request rate scaling, Transfer Acceleration |
| [API Contracts](api-contracts.md) | REST API design, headers, error codes |

---

*Last updated: 2025-01 | Prepared for Amazon system design interviews*
