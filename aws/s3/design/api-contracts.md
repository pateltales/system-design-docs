# Amazon S3 — API Contracts & Design

> Companion deep dive to the [interview simulation](interview-simulation.md). This document details S3's REST API design, request/response formats, and design decisions.

---

## Table of Contents

1. [API Design Philosophy](#1-api-design-philosophy)
2. [Core Object Operations](#2-core-object-operations)
3. [Multipart Upload Operations](#3-multipart-upload-operations)
4. [Bucket Operations](#4-bucket-operations)
5. [URL Styles](#5-url-styles)
6. [Authentication — SigV4](#6-authentication--sigv4-aws-signature-version-4)
7. [Error Handling](#7-error-handling)
8. [Important Headers](#8-important-headers)
9. [Advanced APIs](#9-advanced-apis-brief)
10. [Rate Limits & Throttling](#10-rate-limits--throttling)
11. [Design Decisions Summary](#11-design-decisions-summary)

---

## 1. API Design Philosophy

### 1.1 Why REST over gRPC

S3 chose REST/HTTP as its protocol for several deliberate reasons:

| Factor | REST/HTTP | gRPC/Protobuf |
|--------|-----------|---------------|
| Browser compatibility | Native — any browser can `GET` an object | Requires grpc-web proxy layer |
| CDN integration | Works with every CDN out of the box (CloudFront, Akamai, etc.) | CDNs don't natively proxy gRPC streams |
| Universality | Every language, every platform has an HTTP client | Requires protobuf codegen toolchain |
| Cacheability | HTTP caching semantics (ETag, If-None-Match, Cache-Control) built-in | No built-in caching layer |
| Firewall traversal | Port 443 is universally allowed | HTTP/2 on 443 works, but some proxies strip non-HTTP traffic |
| Debugging | `curl`, browser dev tools, any HTTP tool | Requires specialized tooling (grpcurl, Bloom RPC) |
| Streaming | Chunked transfer-encoding for large objects | Native streaming, but overkill for blob storage |

**The core insight:** S3 is a storage service consumed by the widest possible audience — web
browsers, mobile apps, CLI tools, other AWS services, third-party integrations, IoT devices.
REST/HTTP is the lowest-common-denominator protocol that all of these speak natively. gRPC
would optimize throughput for a narrow set of server-to-server use cases at the cost of
excluding the long tail.

### 1.2 Resource-Oriented Design

S3's API models two resource types:

```
Bucket   →  https://s3.amazonaws.com/{bucket}
Object   →  https://s3.amazonaws.com/{bucket}/{key}
```

Every operation maps to an HTTP method on one of these resources:

```
POST   /                           → CreateBucket (with body)
DELETE /                           → DeleteBucket
PUT    /{key}                      → PutObject
GET    /{key}                      → GetObject
DELETE /{key}                      → DeleteObject
HEAD   /{key}                      → HeadObject
GET    /?list-type=2&prefix=...    → ListObjectsV2
```

This is textbook REST: resources identified by URIs, manipulated through a uniform interface
(GET, PUT, DELETE, HEAD, POST). Query parameters specialize behavior (listing, versioning,
tagging) rather than introducing new verbs.

### 1.3 Idempotency

| Method | Idempotent? | Implication |
|--------|-------------|-------------|
| `PUT` | Yes | Repeating `PUT /my-key` with the same body always results in the same object. Safe to retry on timeout. |
| `GET` | Yes | Read-only — no side effects. Safe to retry unconditionally. |
| `HEAD` | Yes | Same as GET, no body. |
| `DELETE` | Yes | Deleting an already-deleted key returns 204 (not an error). Safe to retry. |
| `POST` | No | `POST` for CreateMultipartUpload generates a new `upload_id` each time. Retrying creates duplicate uploads. |

**Why this matters in interviews:** When the interviewer asks "what happens if the client
times out on a PUT?" you can confidently say: "PUT is idempotent — the client retries the
exact same request and the outcome is identical. The object is written once, or overwritten
with the same content. No corruption, no duplication."

For non-idempotent `POST` (CreateMultipartUpload), the client must check whether a previous
upload was already created (via ListMultipartUploads) before retrying, or simply proceed with
a new upload and abort the orphaned one later via lifecycle rules.

---

## 2. Core Object Operations

### 2.1 PUT Object

**Purpose:** Upload an object to a bucket. Overwrites any existing object at the same key.

#### Request

```http
PUT /photos/2024/sunset.jpg HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Length: 11434
Content-Type: image/jpeg
Content-MD5: pUNO8a4PlVkiknGHsfKcQA==
x-amz-storage-class: STANDARD
x-amz-server-side-encryption: aws:kms
x-amz-server-side-encryption-aws-kms-key-id: arn:aws:kms:us-east-1:123456789012:key/abcd-1234
x-amz-meta-photographer: ashwani
x-amz-meta-location: seattle
x-amz-checksum-sha256: oGexB...base64...==
Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20260212/us-east-1/s3/aws4_request, SignedHeaders=content-length;content-md5;content-type;host;x-amz-content-sha256;x-amz-date;x-amz-meta-location;x-amz-meta-photographer;x-amz-server-side-encryption;x-amz-storage-class, Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
x-amz-date: 20260212T120000Z
x-amz-content-sha256: 44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072

<11434 bytes of JPEG binary data>
```

#### Response — Success

```http
HTTP/1.1 200 OK
x-amz-request-id: 4442587FB7D0A2F9
x-amz-id-2: vlR7PnpV2Ce81l0PRw6jlUpck7aLJmLR5lpF+IEKAN0=
ETag: "1b2cf535f27731c974343645a3985328"
x-amz-version-id: 3HL4kqtJvjVBH40Nrjfkd
x-amz-server-side-encryption: aws:kms
x-amz-server-side-encryption-aws-kms-key-id: arn:aws:kms:us-east-1:123456789012:key/abcd-1234
x-amz-checksum-sha256: oGexB...base64...==
Date: Wed, 12 Feb 2026 12:00:01 GMT
Content-Length: 0
```

#### Status Codes

| Code | Meaning |
|------|---------|
| `200 OK` | Object stored successfully |
| `400 Bad Request` | Malformed request (e.g., invalid Content-MD5 encoding) |
| `403 Forbidden` | Authentication failure or access denied |
| `404 Not Found` | Bucket does not exist |
| `409 Conflict` | Bucket is in a region other than what the request was sent to |
| `500 Internal Server Error` | S3 internal failure — retry with backoff |
| `503 Service Unavailable` | Throttled or service overloaded — retry with backoff |

#### Server-Side Behavior Steps

```
Client sends PUT request
       │
       ▼
┌──────────────┐
│ 1. AUTHENTICATE │  Verify SigV4 signature against IAM credentials.
│              │     Check x-amz-date is within 15-minute clock skew.
│              │     Check x-amz-content-sha256 matches body hash.
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 2. AUTHORIZE │  Evaluate IAM policies, bucket policy, ACLs, S3 Access Points,
│              │  VPC endpoint policies. Any explicit DENY → 403.
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 3. VALIDATE  │  Check Content-MD5 matches body hash (if provided).
│              │  Check Content-Length matches actual body size.
│              │  Check x-amz-checksum-sha256 matches (if provided).
│              │  Check key length <= 1024 bytes (UTF-8 encoded).
│              │  Check object size <= 5 GB (for single PUT; use multipart for larger).
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ 4. STORE DATA│  Write data to the storage tier. For STANDARD class, data is
│              │  replicated across >= 3 AZs before returning. Data is erasure-
│              │  coded (not simple replication) for 11 nines of durability.
└──────┬───────┘
       │
       ▼
┌──────────────────┐
│ 5. UPDATE METADATA│  Write metadata record (key, version_id, ETag, size,
│                  │  storage_class, user-metadata, SSE info) to the metadata
│                  │  subsystem (internal distributed store).
└──────┬───────────┘
       │
       ▼
┌──────────────────┐
│ 6. UPDATE WITNESS│  Confirm that the write is durable — the "witness"
│                  │  quorum confirms replication success across AZs.
└──────┬───────────┘
       │
       ▼
┌──────────────┐
│ 7. RETURN    │  200 OK with ETag, version-id, SSE headers.
│              │  The object is now readable (strong read-after-write
│              │  consistency as of December 2020).
└──────────────┘
```

**Key guarantee:** Since December 2020, S3 provides **strong read-after-write consistency**.
A successful PUT immediately makes the object visible to subsequent GETs and LISTs. There is
no eventual consistency window.

---

### 2.2 GET Object

**Purpose:** Retrieve an object's data and metadata.

#### Basic Request

```http
GET /photos/2024/sunset.jpg HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20260212/us-east-1/s3/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=abcdef1234567890...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

#### Basic Response

```http
HTTP/1.1 200 OK
x-amz-request-id: 4442587FB7D0A2F9
x-amz-id-2: vlR7PnpV2Ce81l0PRw6jlUpck7aLJmLR5lpF+IEKAN0=
Content-Type: image/jpeg
Content-Length: 11434
ETag: "1b2cf535f27731c974343645a3985328"
Last-Modified: Wed, 12 Feb 2026 12:00:01 GMT
x-amz-version-id: 3HL4kqtJvjVBH40Nrjfkd
x-amz-server-side-encryption: aws:kms
x-amz-meta-photographer: ashwani
x-amz-meta-location: seattle
Accept-Ranges: bytes

<11434 bytes of JPEG binary data>
```

#### Byte-Range Fetches (Partial Reads)

The `Range` header enables fetching a specific byte range — critical for large objects,
video streaming, and resuming interrupted downloads.

```http
GET /videos/lecture.mp4 HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Range: bytes=0-1048575
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

```http
HTTP/1.1 206 Partial Content
Content-Type: video/mp4
Content-Length: 1048576
Content-Range: bytes 0-1048575/524288000
ETag: "9b2cf535f27731c974343645a3985328"
Accept-Ranges: bytes

<1048576 bytes of video data — first 1 MB>
```

**Multiple ranges in one request:**

```http
GET /data/report.csv HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Range: bytes=0-999, 5000-5999
Authorization: AWS4-HMAC-SHA256 ...
```

```http
HTTP/1.1 206 Partial Content
Content-Type: multipart/byteranges; boundary=THIS_STRING_SEPARATES

--THIS_STRING_SEPARATES
Content-Type: text/csv
Content-Range: bytes 0-999/20000

<first 1000 bytes>
--THIS_STRING_SEPARATES
Content-Type: text/csv
Content-Range: bytes 5000-5999/20000

<bytes 5000-5999>
--THIS_STRING_SEPARATES--
```

#### Conditional Requests

These headers let the client avoid transferring data that hasn't changed, saving bandwidth:

| Header | Behavior |
|--------|----------|
| `If-Modified-Since: <date>` | Returns `304 Not Modified` if the object hasn't changed since that date |
| `If-Unmodified-Since: <date>` | Returns `412 Precondition Failed` if the object has been modified |
| `If-Match: "<etag>"` | Returns the object only if its ETag matches; otherwise `412` |
| `If-None-Match: "<etag>"` | Returns `304 Not Modified` if the ETag matches (object unchanged) |

```http
GET /photos/2024/sunset.jpg HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
If-None-Match: "1b2cf535f27731c974343645a3985328"
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

```http
HTTP/1.1 304 Not Modified
ETag: "1b2cf535f27731c974343645a3985328"
x-amz-request-id: 4442587FB7D0A2F9
Date: Wed, 12 Feb 2026 13:00:00 GMT
```

#### Versioned GET

To fetch a specific version of an object:

```http
GET /photos/2024/sunset.jpg?versionId=3HL4kqtJvjVBH40Nrjfkd HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
```

#### Status Codes

| Code | Meaning |
|------|---------|
| `200 OK` | Full object returned |
| `206 Partial Content` | Byte-range returned successfully |
| `304 Not Modified` | Conditional request — object hasn't changed |
| `403 Forbidden` | Access denied |
| `404 Not Found` | Object key does not exist (or bucket doesn't exist) |
| `412 Precondition Failed` | `If-Match` or `If-Unmodified-Since` failed |
| `416 Range Not Satisfiable` | Requested byte range exceeds object size |

---

### 2.3 DELETE Object

**Purpose:** Remove an object from a bucket.

#### Request

```http
DELETE /photos/2024/sunset.jpg HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

#### Behavior: Versioning Disabled

When versioning is **not enabled** on the bucket:

```http
HTTP/1.1 204 No Content
x-amz-request-id: 4442587FB7D0A2F9
x-amz-id-2: vlR7PnpV2Ce81l0PRw6jlUpck7aLJmLR5lpF+IEKAN0=
Date: Wed, 12 Feb 2026 14:00:00 GMT
```

The object is **permanently deleted**. Subsequent `GET` requests return `404 Not Found`.

**Idempotency:** Deleting an already-deleted key also returns `204 No Content` (not `404`).
This is deliberate — it makes DELETE idempotent and safe to retry on timeouts.

#### Behavior: Versioning Enabled

When versioning is **enabled**, DELETE does not permanently remove the object. Instead, S3
inserts a **delete marker** — a zero-byte placeholder that becomes the current version.

```http
HTTP/1.1 204 No Content
x-amz-request-id: 4442587FB7D0A2F9
x-amz-delete-marker: true
x-amz-version-id: UIORUnfndfiufdisojhr398493jfdkjd
Date: Wed, 12 Feb 2026 14:00:00 GMT
```

After this:
- `GET /photos/2024/sunset.jpg` returns `404 Not Found` (the delete marker is the "current" version)
- `GET /photos/2024/sunset.jpg?versionId=3HL4kqtJvjVBH40Nrjfkd` still returns the original object
- The previous versions are preserved and recoverable

#### Permanently Delete a Specific Version

```http
DELETE /photos/2024/sunset.jpg?versionId=3HL4kqtJvjVBH40Nrjfkd HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
```

This permanently removes that specific version — no delete marker, actual deletion.

#### Batch Delete (Delete Multiple Objects)

```http
POST /?delete HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-MD5: lMzFig8mJhQMzK1PVpOrOw==
Content-Length: 352
Authorization: AWS4-HMAC-SHA256 ...

<?xml version="1.0" encoding="UTF-8"?>
<Delete>
  <Quiet>false</Quiet>
  <Object>
    <Key>photos/2024/sunset.jpg</Key>
  </Object>
  <Object>
    <Key>photos/2024/sunrise.jpg</Key>
    <VersionId>UIORUnfndfiufdisojhr398493jfdkjd</VersionId>
  </Object>
  <Object>
    <Key>photos/2024/mountain.jpg</Key>
  </Object>
</Delete>
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<DeleteResult>
  <Deleted>
    <Key>photos/2024/sunset.jpg</Key>
    <DeleteMarker>true</DeleteMarker>
    <DeleteMarkerVersionId>A2s6G8h...</DeleteMarkerVersionId>
  </Deleted>
  <Deleted>
    <Key>photos/2024/sunrise.jpg</Key>
    <VersionId>UIORUnfndfiufdisojhr398493jfdkjd</VersionId>
  </Deleted>
  <Error>
    <Key>photos/2024/mountain.jpg</Key>
    <Code>AccessDenied</Code>
    <Message>Access Denied</Message>
  </Error>
</DeleteResult>
```

Note: Batch delete can return `200 OK` at the HTTP level even if individual objects failed.
You **must** parse the response body to check for per-object errors.

---

### 2.4 HEAD Object

**Purpose:** Retrieve an object's metadata without downloading the body. Identical to GET
in every way except no response body is returned.

#### Request

```http
HEAD /photos/2024/sunset.jpg HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

#### Response

```http
HTTP/1.1 200 OK
Content-Type: image/jpeg
Content-Length: 11434
ETag: "1b2cf535f27731c974343645a3985328"
Last-Modified: Wed, 12 Feb 2026 12:00:01 GMT
x-amz-version-id: 3HL4kqtJvjVBH40Nrjfkd
x-amz-server-side-encryption: aws:kms
x-amz-storage-class: STANDARD
x-amz-meta-photographer: ashwani
x-amz-meta-location: seattle
Accept-Ranges: bytes
x-amz-request-id: 4442587FB7D0A2F9
```

**Use cases:**
- Check if an object exists before downloading (cheaper than GET + discard)
- Retrieve Content-Length to plan byte-range downloads
- Check ETag to determine if local cache is stale
- Read user-defined metadata (x-amz-meta-*) without transferring data
- Check storage class before making lifecycle decisions

---

### 2.5 LIST Objects v2 (GET Bucket)

**Purpose:** Enumerate objects in a bucket with optional prefix filtering and delimiter-based
grouping (to simulate directory listing).

#### Request

```http
GET /?list-type=2&prefix=photos/2024/&delimiter=/&max-keys=1000 HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

#### Response

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>my-bucket</Name>
  <Prefix>photos/2024/</Prefix>
  <Delimiter>/</Delimiter>
  <MaxKeys>1000</MaxKeys>
  <KeyCount>3</KeyCount>
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>1ueGcxLPRx1Tr/XYExHnhbYLgveDs2J/wm36Hy4vbOwM=</NextContinuationToken>
  <Contents>
    <Key>photos/2024/sunset.jpg</Key>
    <LastModified>2026-02-12T12:00:01.000Z</LastModified>
    <ETag>&quot;1b2cf535f27731c974343645a3985328&quot;</ETag>
    <Size>11434</Size>
    <StorageClass>STANDARD</StorageClass>
  </Contents>
  <Contents>
    <Key>photos/2024/sunrise.jpg</Key>
    <LastModified>2026-02-10T08:30:00.000Z</LastModified>
    <ETag>&quot;a3bf4abc931cc9f60e8e53e04d39b432&quot;</ETag>
    <Size>8921</Size>
    <StorageClass>STANDARD</StorageClass>
  </Contents>
  <Contents>
    <Key>photos/2024/mountain.jpg</Key>
    <LastModified>2026-02-08T15:45:22.000Z</LastModified>
    <ETag>&quot;7dc950c07d6bc0e5ad2c66a95bf13e96&quot;</ETag>
    <Size>24500</Size>
    <StorageClass>STANDARD_IA</StorageClass>
  </Contents>
  <CommonPrefixes>
    <Prefix>photos/2024/january/</Prefix>
  </CommonPrefixes>
  <CommonPrefixes>
    <Prefix>photos/2024/february/</Prefix>
  </CommonPrefixes>
</ListBucketResult>
```

#### How Prefix + Delimiter Simulates Directories

S3 has a **flat namespace** — there are no real directories. But prefix and delimiter
parameters simulate a directory hierarchy:

```
Bucket contents (flat list of keys):
  photos/2024/sunset.jpg
  photos/2024/sunrise.jpg
  photos/2024/mountain.jpg
  photos/2024/january/snow.jpg
  photos/2024/january/ice.jpg
  photos/2024/february/rain.jpg
  documents/report.pdf

Query: prefix=photos/2024/  delimiter=/

Result:
  Contents (objects directly "in" photos/2024/):
    photos/2024/sunset.jpg
    photos/2024/sunrise.jpg
    photos/2024/mountain.jpg
  CommonPrefixes (simulated "subdirectories"):
    photos/2024/january/
    photos/2024/february/
```

The **delimiter** (`/`) tells S3: "for any key that has the prefix `photos/2024/`, if there's
another `/` after the prefix, group everything up to that `/` into a CommonPrefix entry
instead of listing individual objects." This is exactly how a directory listing works —
show files in the current directory and names of subdirectories.

#### Pagination with Continuation Tokens

When `IsTruncated` is `true`, the response includes `NextContinuationToken`. Use it to fetch
the next page:

```http
GET /?list-type=2&prefix=photos/2024/&delimiter=/&max-keys=1000&continuation-token=1ueGcxLPRx1Tr/XYExHnhbYLgveDs2J/wm36Hy4vbOwM= HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
```

**Why continuation tokens instead of offset-based pagination?**

| Approach | Problem |
|----------|---------|
| Offset (`?offset=1000`) | If objects are added/deleted between pages, you skip or duplicate entries |
| Continuation token | The token encodes the position in the index; concurrent writes don't cause skips or duplicates |

Continuation tokens provide **consistent pagination** in the face of concurrent writes —
a property that offset-based pagination cannot guarantee.

---

## 3. Multipart Upload Operations

Multipart upload is required for objects larger than 5 GB and recommended for objects
larger than 100 MB. It provides parallel upload, resumability, and per-part integrity
checking.

### 3.1 Constraints

| Constraint | Value |
|------------|-------|
| Minimum part size | 5 MB (except the last part) |
| Maximum part size | 5 GB |
| Maximum number of parts | 10,000 |
| Maximum object size | 5 TB (10,000 parts x 5 GB theoretically, but capped at 5 TB) |
| Upload ID expiration | None — but orphaned uploads should be cleaned via lifecycle rules |

### 3.2 CreateMultipartUpload

**Purpose:** Initiate a multipart upload and obtain an `upload_id`.

```http
POST /videos/lecture.mp4?uploads HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Type: video/mp4
x-amz-storage-class: STANDARD
x-amz-server-side-encryption: aws:kms
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<InitiateMultipartUploadResult>
  <Bucket>my-bucket</Bucket>
  <Key>videos/lecture.mp4</Key>
  <UploadId>VXBsb2FkIElEIGZvciBlbHZpbmcncyBteS1tb3ZpZS5tMnRzIHVwbG9hZA</UploadId>
</InitiateMultipartUploadResult>
```

**Important:** This is a `POST` (not idempotent). Each call generates a new `upload_id`.
If you retry on timeout, you may create orphaned uploads that consume storage until aborted
or cleaned by lifecycle policy.

### 3.3 UploadPart

**Purpose:** Upload one part of the multipart upload.

```http
PUT /videos/lecture.mp4?partNumber=1&uploadId=VXBsb2FkIElEIGZvciBlbHZpbmcncyBteS1tb3ZpZS5tMnRzIHVwbG9hZA HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Length: 10485760
Content-MD5: pUNO8a4PlVkiknGHsfKcQA==
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120100Z
x-amz-content-sha256: 7a1c9d3b2e...

<10485760 bytes of binary data — 10 MB>
```

```http
HTTP/1.1 200 OK
ETag: "b54357faf0632cce46e942fa68356b38"
x-amz-request-id: 4442587FB7D0A2F9
Date: Wed, 12 Feb 2026 12:01:01 GMT
```

**Key points:**
- Part numbers range from 1 to 10,000
- Each part (except the last) must be at least 5 MB
- The returned `ETag` must be recorded — it's required for CompleteMultipartUpload
- Parts can be uploaded in parallel for maximum throughput
- Re-uploading the same part number overwrites the previous upload for that part

### 3.4 CompleteMultipartUpload

**Purpose:** Assemble all parts into the final object.

```http
POST /videos/lecture.mp4?uploadId=VXBsb2FkIElEIGZvciBlbHZpbmcncyBteS1tb3ZpZS5tMnRzIHVwbG9hZA HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Type: application/xml
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T121000Z
x-amz-content-sha256: 3e4a1c...

<?xml version="1.0" encoding="UTF-8"?>
<CompleteMultipartUpload>
  <Part>
    <PartNumber>1</PartNumber>
    <ETag>"b54357faf0632cce46e942fa68356b38"</ETag>
  </Part>
  <Part>
    <PartNumber>2</PartNumber>
    <ETag>"acbd18db4cc2f85cedef654fccc4a4d8"</ETag>
  </Part>
  <Part>
    <PartNumber>3</PartNumber>
    <ETag>"37b51d194a7513e45b56f6524f2d51f2"</ETag>
  </Part>
</CompleteMultipartUpload>
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<CompleteMultipartUploadResult>
  <Location>https://my-bucket.s3.us-east-1.amazonaws.com/videos/lecture.mp4</Location>
  <Bucket>my-bucket</Bucket>
  <Key>videos/lecture.mp4</Key>
  <ETag>"4d9320c13b784b09a9932b3c521d36a2-3"</ETag>
</CompleteMultipartUploadResult>
```

**ETag for multipart objects:** The ETag is `"<MD5-of-concatenated-part-ETags>-<part-count>"`.
This is **not** the MD5 of the full object — it's an opaque identifier. The `-3` suffix
indicates 3 parts were used.

**Critical warning:** CompleteMultipartUpload can return `200 OK` with an error in the XML
body. You **must** parse the response body. A common pitfall is treating any `200` as success.

### 3.5 AbortMultipartUpload

**Purpose:** Cancel an in-progress multipart upload and delete all uploaded parts.

```http
DELETE /videos/lecture.mp4?uploadId=VXBsb2FkIElEIGZvciBlbHZpbmcncyBteS1tb3ZpZS5tMnRzIHVwbG9hZA HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T130000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

```http
HTTP/1.1 204 No Content
x-amz-request-id: 4442587FB7D0A2F9
Date: Wed, 12 Feb 2026 13:00:01 GMT
```

**Best practice:** Set a lifecycle rule to automatically abort incomplete multipart uploads
after N days (e.g., 7 days). Orphaned parts continue to incur storage charges.

### 3.6 ListParts

**Purpose:** List the parts that have been uploaded for a specific multipart upload.

```http
GET /videos/lecture.mp4?uploadId=VXBsb2FkIElEIGZvciBlbHZpbmcncyBteS1tb3ZpZS5tMnRzIHVwbG9hZA HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<ListPartsResult>
  <Bucket>my-bucket</Bucket>
  <Key>videos/lecture.mp4</Key>
  <UploadId>VXBsb2FkIElEIGZvciBlbHZpbmcncyBteS1tb3ZpZS5tMnRzIHVwbG9hZA</UploadId>
  <PartNumberMarker>0</PartNumberMarker>
  <NextPartNumberMarker>3</NextPartNumberMarker>
  <MaxParts>1000</MaxParts>
  <IsTruncated>false</IsTruncated>
  <Part>
    <PartNumber>1</PartNumber>
    <LastModified>2026-02-12T12:01:01.000Z</LastModified>
    <ETag>"b54357faf0632cce46e942fa68356b38"</ETag>
    <Size>10485760</Size>
  </Part>
  <Part>
    <PartNumber>2</PartNumber>
    <LastModified>2026-02-12T12:02:01.000Z</LastModified>
    <ETag>"acbd18db4cc2f85cedef654fccc4a4d8"</ETag>
    <Size>10485760</Size>
  </Part>
  <Part>
    <PartNumber>3</PartNumber>
    <LastModified>2026-02-12T12:03:01.000Z</LastModified>
    <ETag>"37b51d194a7513e45b56f6524f2d51f2"</ETag>
    <Size>5242880</Size>
  </Part>
</ListPartsResult>
```

### 3.7 Multipart Upload Flow Diagram

```
Client                                        S3
  │                                            │
  │  POST /key?uploads                         │
  │ ─────────────────────────────────────────► │
  │                                            │
  │  200 OK  {upload_id: "abc123"}             │
  │ ◄───────────────────────────────────────── │
  │                                            │
  │  PUT /key?partNumber=1&uploadId=abc123     │  ┐
  │ ─────────────────────────────────────────► │  │
  │  200 OK  ETag: "etag1"                     │  │
  │ ◄───────────────────────────────────────── │  │
  │                                            │  │ Parallel
  │  PUT /key?partNumber=2&uploadId=abc123     │  │ uploads
  │ ─────────────────────────────────────────► │  │ possible
  │  200 OK  ETag: "etag2"                     │  │
  │ ◄───────────────────────────────────────── │  │
  │                                            │  │
  │  PUT /key?partNumber=3&uploadId=abc123     │  │
  │ ─────────────────────────────────────────► │  │
  │  200 OK  ETag: "etag3"                     │  │
  │ ◄───────────────────────────────────────── │  ┘
  │                                            │
  │  POST /key?uploadId=abc123                 │
  │  Body: [(1,etag1), (2,etag2), (3,etag3)]  │
  │ ─────────────────────────────────────────► │
  │                                            │
  │  200 OK  {Location, ETag}                  │
  │ ◄───────────────────────────────────────── │
  │                                            │
```

---

## 4. Bucket Operations

### 4.1 CreateBucket (PUT /)

```http
PUT / HTTP/1.1
Host: my-new-bucket.s3.amazonaws.com
Content-Length: 137
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: 3c4a1f...

<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <LocationConstraint>us-west-2</LocationConstraint>
</CreateBucketConfiguration>
```

```http
HTTP/1.1 200 OK
Location: /my-new-bucket
x-amz-request-id: 4442587FB7D0A2F9
Date: Wed, 12 Feb 2026 12:00:00 GMT
```

**Naming rules:**
- 3-63 characters
- Lowercase letters, numbers, hyphens only
- Must start with a letter or number
- Cannot be formatted as an IP address (e.g., 192.168.0.1)
- **Globally unique** across all of S3 (not just your account)

**Status codes:**

| Code | Meaning |
|------|---------|
| `200 OK` | Bucket created |
| `409 BucketAlreadyExists` | Name taken by another account |
| `409 BucketAlreadyOwnedByYou` | You already own this bucket |

### 4.2 DeleteBucket (DELETE /)

```http
DELETE / HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

```http
HTTP/1.1 204 No Content
x-amz-request-id: 4442587FB7D0A2F9
Date: Wed, 12 Feb 2026 12:00:00 GMT
```

**Precondition:** The bucket **must be empty** (no objects, no object versions, no delete
markers, no incomplete multipart uploads). If not empty, S3 returns `409 BucketNotEmpty`.

### 4.3 ListBuckets (GET /)

```http
GET / HTTP/1.1
Host: s3.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<ListAllMyBucketsResult>
  <Owner>
    <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
    <DisplayName>ashwani</DisplayName>
  </Owner>
  <Buckets>
    <Bucket>
      <Name>my-bucket</Name>
      <CreationDate>2026-01-15T10:30:00.000Z</CreationDate>
    </Bucket>
    <Bucket>
      <Name>my-backup-bucket</Name>
      <CreationDate>2026-02-01T08:00:00.000Z</CreationDate>
    </Bucket>
  </Buckets>
</ListAllMyBucketsResult>
```

### 4.4 GetBucketLocation

```http
GET /?location HTTP/1.1
Host: my-bucket.s3.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">us-west-2</LocationConstraint>
```

For buckets in `us-east-1`, the response body is empty (historical quirk):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>
```

### 4.5 Bucket Versioning

#### Enable Versioning

```http
PUT /?versioning HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Length: 124
Authorization: AWS4-HMAC-SHA256 ...

<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Status>Enabled</Status>
</VersioningConfiguration>
```

```http
HTTP/1.1 200 OK
```

#### Get Versioning Status

```http
GET /?versioning HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Status>Enabled</Status>
</VersioningConfiguration>
```

**Versioning states:**
- **Unversioned** (default) — no `<Status>` element in the response
- **Enabled** — all new objects get version IDs; old versions preserved
- **Suspended** — new objects get version ID `null`; existing versions preserved

**Important:** Once versioning is enabled, it can be suspended but **never fully disabled**.
This is a one-way door — bucket versioning cannot return to the "unversioned" state.

---

## 5. URL Styles

### 5.1 Virtual-Hosted Style (Preferred)

```
https://{bucket}.s3.{region}.amazonaws.com/{key}
```

Examples:

```
https://my-bucket.s3.us-east-1.amazonaws.com/photos/2024/sunset.jpg
https://data-lake.s3.eu-west-1.amazonaws.com/logs/2026/02/12/access.log
```

### 5.2 Path Style (Deprecated)

```
https://s3.{region}.amazonaws.com/{bucket}/{key}
```

Examples:

```
https://s3.us-east-1.amazonaws.com/my-bucket/photos/2024/sunset.jpg
https://s3.eu-west-1.amazonaws.com/data-lake/logs/2026/02/12/access.log
```

Path-style was deprecated on September 30, 2020 for new buckets. Existing buckets created
before that date continue to work with path-style.

### 5.3 S3 Transfer Acceleration

```
https://{bucket}.s3-accelerate.amazonaws.com/{key}
```

Example:

```
https://my-bucket.s3-accelerate.amazonaws.com/videos/lecture.mp4
```

Transfer Acceleration uses CloudFront edge locations to route uploads over AWS's optimized
backbone network instead of the public internet. Useful for long-distance uploads (e.g.,
uploading from Asia to a bucket in us-east-1).

### 5.4 Why Virtual-Hosted Style Won

```
                    Path-style routing                    Virtual-hosted routing
                    ─────────────────                     ──────────────────────

Client ──► s3.amazonaws.com ──► S3 Front End            Client ──► my-bucket.s3.amazonaws.com
                  │              parses path                              │
                  │              to find bucket                           ▼
                  ▼                                      DNS resolves to S3 Front End
           Routes to bucket                              that ALREADY KNOWS the bucket
           storage partition                             ──► Routes directly to storage partition
```

| Factor | Virtual-Hosted | Path-Style |
|--------|---------------|------------|
| DNS routing | Each bucket resolves to a specific endpoint — enables per-bucket load balancing | All buckets share one hostname — single point of routing |
| CDN caching | CloudFront can cache per-bucket hostname — independent cache namespaces | All buckets share the same CDN cache namespace — harder to manage |
| SSL certificates | Works with wildcard cert `*.s3.amazonaws.com` | Works but bucket name is in the URL path, not the hostname |
| Bucket naming | Bucket name must be DNS-compliant (lowercase, no underscores) | Bucket name can contain uppercase, underscores (legacy) |
| CORS | `Origin` header checking against bucket hostname is cleaner | Bucket identity not in the hostname makes CORS configuration messier |

---

## 6. Authentication — SigV4 (AWS Signature Version 4)

### 6.1 How SigV4 Works

SigV4 is a request-signing protocol. Credentials are **never transmitted** — instead, the
client signs the request with a derived key and the server independently computes the same
signature to verify authenticity.

```
Step 1: Create Canonical Request
─────────────────────────────────
  HTTP Method + \n
  Canonical URI + \n
  Canonical Query String + \n
  Canonical Headers + \n
  Signed Headers + \n
  Hashed Payload (SHA-256 of body)

  Example:
  ┌──────────────────────────────────────────┐
  │ PUT                                      │
  │ /photos/2024/sunset.jpg                  │
  │                                          │  (empty query string)
  │ content-type:image/jpeg                  │
  │ host:my-bucket.s3.us-east-1.amazonaws.com│
  │ x-amz-content-sha256:44ce7dd67c...      │
  │ x-amz-date:20260212T120000Z             │
  │                                          │
  │ content-type;host;x-amz-content-sha256;  │
  │ x-amz-date                               │
  │                                          │
  │ 44ce7dd67c959e0d3524ffac1771dfbba87d...  │
  └──────────────────────────────────────────┘

Step 2: Create String to Sign
──────────────────────────────
  "AWS4-HMAC-SHA256" + \n
  Timestamp (ISO 8601) + \n
  Scope (date/region/service/aws4_request) + \n
  SHA-256(Canonical Request)

  Example:
  ┌──────────────────────────────────────────┐
  │ AWS4-HMAC-SHA256                         │
  │ 20260212T120000Z                         │
  │ 20260212/us-east-1/s3/aws4_request       │
  │ 7344ae5b7ee6c3e7e6b0fe0640412a37...     │
  └──────────────────────────────────────────┘

Step 3: Derive Signing Key
───────────────────────────
  kDate    = HMAC-SHA256("AWS4" + SecretKey,  Date)
  kRegion  = HMAC-SHA256(kDate,               Region)
  kService = HMAC-SHA256(kRegion,             Service)
  kSigning = HMAC-SHA256(kService,            "aws4_request")

  ┌──────────────────────────────────────────────────────────┐
  │  HMAC("AWS4"+secret, "20260212")                         │
  │      └──► HMAC(result, "us-east-1")                      │
  │              └──► HMAC(result, "s3")                      │
  │                      └──► HMAC(result, "aws4_request")    │
  │                              └──► signing_key             │
  └──────────────────────────────────────────────────────────┘

Step 4: Calculate Signature
────────────────────────────
  signature = HEX(HMAC-SHA256(kSigning, StringToSign))
```

### 6.2 Authorization Header Format

```
Authorization: AWS4-HMAC-SHA256
  Credential=AKIAIOSFODNN7EXAMPLE/20260212/us-east-1/s3/aws4_request,
  SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date,
  Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
```

### 6.3 Required SigV4 Headers

| Header | Purpose |
|--------|---------|
| `Authorization` | Contains algorithm, credential scope, signed headers list, and signature |
| `x-amz-date` | Request timestamp (ISO 8601 format: `20260212T120000Z`). Clock skew tolerance: 15 minutes. |
| `x-amz-content-sha256` | SHA-256 hash of the request body. For streaming uploads, use `STREAMING-AWS4-HMAC-SHA256-PAYLOAD`. For unsigned payload, use `UNSIGNED-PAYLOAD`. |

### 6.4 Presigned URLs

Presigned URLs allow unauthenticated clients (browsers, mobile apps, external partners) to
perform a specific S3 operation for a limited time, without needing AWS credentials.

#### How a Presigned URL Looks

```
https://my-bucket.s3.us-east-1.amazonaws.com/photos/2024/sunset.jpg
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260212%2Fus-east-1%2Fs3%2Faws4_request
  &X-Amz-Date=20260212T120000Z
  &X-Amz-Expires=3600
  &X-Amz-SignedHeaders=host
  &X-Amz-Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
```

#### Query String Parameters

| Parameter | Description |
|-----------|-------------|
| `X-Amz-Algorithm` | Always `AWS4-HMAC-SHA256` |
| `X-Amz-Credential` | `{access_key}/{date}/{region}/{service}/aws4_request` (URL-encoded) |
| `X-Amz-Date` | Timestamp when the URL was signed |
| `X-Amz-Expires` | Validity period in seconds (1 to 604800 = 7 days max) |
| `X-Amz-SignedHeaders` | Which headers are included in the signature (at minimum: `host`) |
| `X-Amz-Signature` | The computed signature |

#### Presigned URL for Upload (PUT)

A server can generate a presigned PUT URL and give it to a client to upload directly to S3,
bypassing the server entirely:

```
https://my-bucket.s3.us-east-1.amazonaws.com/uploads/user123/avatar.jpg
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260212%2Fus-east-1%2Fs3%2Faws4_request
  &X-Amz-Date=20260212T120000Z
  &X-Amz-Expires=300
  &X-Amz-SignedHeaders=host;content-type
  &X-Amz-Signature=abc123...
```

The client then does:

```http
PUT /uploads/user123/avatar.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256&... HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Type: image/jpeg
Content-Length: 45000

<45000 bytes of image data>
```

#### Use Cases

```
┌──────────────────────────────────────────────────────────────────┐
│                     Presigned URL Flow                            │
│                                                                  │
│  Client ──(1) Request URL──► App Server                          │
│                                  │                               │
│                          (2) Generate presigned URL               │
│                              (using AWS credentials)             │
│                                  │                               │
│  Client ◄──(3) Return URL────── App Server                       │
│     │                                                            │
│     └──(4) PUT/GET directly──► S3                                │
│                                  │                               │
│         No credentials ever     (5) Verify signature             │
│         leave the server             └──► 200 OK or 403          │
└──────────────────────────────────────────────────────────────────┘
```

- **Share a private object** — generate a GET presigned URL with 1-hour expiry; share via
  email/Slack
- **Direct browser upload** — generate a PUT presigned URL; the browser uploads directly to
  S3 without proxying through your server, saving bandwidth and latency
- **Temporary download links** — e-commerce receipt PDFs, export files, report downloads

#### Security Properties

| Property | Detail |
|----------|--------|
| Credentials never transmitted | The URL contains the credential *scope* (access key + date + region), not the secret key |
| Time-limited | Expires after the specified duration (max 7 days) |
| Operation-scoped | A presigned GET URL cannot be used for PUT or DELETE |
| Key-scoped | The URL is bound to a specific object key |
| Replay protection | The timestamp + expiry window prevents indefinite reuse |
| Revocation | Revoking the IAM user/role's credentials invalidates all presigned URLs generated with those credentials |

---

## 7. Error Handling

### 7.1 Error Response Format

All S3 errors return an XML body with a consistent structure:

```http
HTTP/1.1 404 Not Found
Content-Type: application/xml
x-amz-request-id: 4442587FB7D0A2F9
x-amz-id-2: vlR7PnpV2Ce81l0PRw6jlUpck7aLJmLR5lpF+IEKAN0=

<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchKey</Code>
  <Message>The specified key does not exist.</Message>
  <Key>photos/2024/nonexistent.jpg</Key>
  <RequestId>4442587FB7D0A2F9</RequestId>
  <HostId>vlR7PnpV2Ce81l0PRw6jlUpck7aLJmLR5lpF+IEKAN0=</HostId>
</Error>
```

### 7.2 S3 Error Code Reference

| Error Code | HTTP Status | Description | Retryable? |
|------------|-------------|-------------|------------|
| `NoSuchKey` | 404 | The specified object key does not exist | No |
| `NoSuchBucket` | 404 | The specified bucket does not exist | No |
| `NoSuchUpload` | 404 | The specified multipart upload ID does not exist | No |
| `BucketAlreadyExists` | 409 | The bucket name is already taken by another account | No |
| `BucketAlreadyOwnedByYou` | 409 | You already own this bucket | No |
| `BucketNotEmpty` | 409 | Cannot delete a bucket that contains objects | No |
| `AccessDenied` | 403 | IAM/bucket policy denies this operation | No |
| `SignatureDoesNotMatch` | 403 | The SigV4 signature is invalid (wrong key, wrong canonical request, clock skew) | No (fix the signature) |
| `InvalidAccessKeyId` | 403 | The access key ID does not exist | No |
| `ExpiredToken` | 400 | STS temporary credentials have expired | No (refresh token) |
| `MalformedXML` | 400 | The request body XML is not well-formed | No (fix the XML) |
| `InvalidBucketName` | 400 | Bucket name does not meet naming requirements | No |
| `EntityTooLarge` | 400 | Object exceeds maximum size (5 GB for single PUT) | No (use multipart) |
| `EntityTooSmall` | 400 | Multipart upload part is smaller than 5 MB (except last) | No (use larger parts) |
| `InvalidPart` | 400 | Part ETag in CompleteMultipartUpload does not match | No |
| `InvalidPartOrder` | 400 | Parts in CompleteMultipartUpload are not in ascending order | No |
| `MethodNotAllowed` | 405 | HTTP method not allowed on this resource | No |
| `PreconditionFailed` | 412 | If-Match or If-Unmodified-Since condition failed | No (conditional) |
| `InvalidRange` | 416 | The Range header requests bytes beyond the object size | No |
| `SlowDown` | 503 | Request rate is too high — throttled | **Yes** — backoff and retry |
| `InternalError` | 500 | S3 internal failure | **Yes** — backoff and retry |
| `ServiceUnavailable` | 503 | S3 is temporarily unable to handle the request | **Yes** — backoff and retry |
| `RequestTimeout` | 400 | Connection idle too long or socket timeout | **Yes** — retry |
| `RequestTimeTooSkewed` | 403 | Clock skew between client and server exceeds 15 minutes | No (fix your clock) |

### 7.3 Retry Strategy

For retryable errors (5xx, 503 SlowDown, 500 InternalError), AWS SDKs implement:

**Exponential Backoff with Full Jitter:**

```
base_delay = 100ms
max_delay  = 20s
max_retries = 5 (typical SDK default, configurable)

for attempt in 1..max_retries:
    delay = min(max_delay, base_delay * 2^attempt)
    jittered_delay = random(0, delay)     ← full jitter
    sleep(jittered_delay)
    retry the request
```

**Why jitter?** Without jitter, if 1,000 clients are all throttled at the same time, they
all retry at the same intervals (100ms, 200ms, 400ms...), creating synchronized thundering
herds. Jitter spreads the retries across the time window.

```
Without jitter (thundering herd):       With full jitter (distributed):
  ┃ ┃ ┃ ┃ ┃ ← all retry at 100ms         ┃   ┃  ┃ ┃   ┃ ← spread across 0-100ms
  ┃ ┃ ┃ ┃ ┃ ← all retry at 200ms           ┃ ┃  ┃   ┃┃  ← spread across 0-200ms
  ┃ ┃ ┃ ┃ ┃ ← all retry at 400ms         ┃    ┃┃   ┃  ┃  ← spread across 0-400ms
```

---

## 8. Important Headers

### 8.1 S3-Specific Request/Response Headers

| Header | Direction | Values | Purpose |
|--------|-----------|--------|---------|
| `x-amz-server-side-encryption` | Req/Resp | `AES256`, `aws:kms`, `aws:kms:dsse` | Server-side encryption algorithm. `AES256` = S3-managed keys (SSE-S3). `aws:kms` = KMS-managed keys (SSE-KMS). `aws:kms:dsse` = dual-layer SSE with KMS. |
| `x-amz-server-side-encryption-aws-kms-key-id` | Req/Resp | KMS key ARN | The KMS key ID used for SSE-KMS encryption |
| `x-amz-storage-class` | Req/Resp | `STANDARD`, `STANDARD_IA`, `ONEZONE_IA`, `INTELLIGENT_TIERING`, `GLACIER_IR`, `GLACIER`, `DEEP_ARCHIVE` | Storage class for the object |
| `x-amz-version-id` | Resp | Version string | The version ID assigned to the object (when versioning is enabled) |
| `x-amz-delete-marker` | Resp | `true` | Present when the "current version" is a delete marker |
| `x-amz-request-id` | Resp | Unique ID | Unique identifier for this request — essential for AWS support troubleshooting |
| `x-amz-id-2` | Resp | Extended ID | Extended request ID — used alongside `x-amz-request-id` for support cases |
| `x-amz-checksum-sha256` | Req/Resp | Base64-encoded SHA-256 | Additional integrity checksum (newer, recommended over Content-MD5) |
| `x-amz-checksum-crc32c` | Req/Resp | Base64-encoded CRC32C | CRC32C checksum — faster to compute than SHA-256 |
| `x-amz-checksum-crc32` | Req/Resp | Base64-encoded CRC32 | CRC32 checksum |
| `x-amz-checksum-sha1` | Req/Resp | Base64-encoded SHA-1 | SHA-1 checksum |
| `x-amz-checksum-algorithm` | Req | `SHA256`, `CRC32C`, `CRC32`, `SHA1` | Which checksum algorithm is being used |
| `x-amz-content-sha256` | Req | SHA-256 hex string or `UNSIGNED-PAYLOAD` | SHA-256 of the payload — required for SigV4. Use `UNSIGNED-PAYLOAD` to skip body hashing for presigned URLs. |
| `x-amz-copy-source` | Req | `/{bucket}/{key}?versionId=...` | Source object for CopyObject — a PUT with this header performs server-side copy |
| `x-amz-metadata-directive` | Req | `COPY`, `REPLACE` | For CopyObject — whether to copy source metadata or replace with new metadata |
| `x-amz-tagging` | Req | URL-encoded tags | Object tags as URL-encoded key=value pairs (e.g., `env=prod&team=data`) |
| `x-amz-meta-{name}` | Req/Resp | Any string | User-defined metadata. Up to 2 KB total across all user metadata headers. |

### 8.2 Standard HTTP Headers Used by S3

| Header | Direction | Purpose in S3 Context |
|--------|-----------|----------------------|
| `Content-MD5` | Req | Base64-encoded MD5 of the body — legacy integrity check. S3 verifies the body against this hash and returns `400 BadDigest` on mismatch. |
| `Content-Type` | Req/Resp | MIME type of the object (e.g., `image/jpeg`, `application/pdf`). S3 stores this as metadata and returns it on GET. |
| `Content-Length` | Req/Resp | Size of the body in bytes. Required for PUT unless using chunked transfer encoding. |
| `Content-Encoding` | Req/Resp | If the object is stored in compressed form (e.g., `gzip`), this header tells clients to decompress. |
| `Content-Disposition` | Req/Resp | Suggests a filename for downloads (e.g., `attachment; filename="report.pdf"`). |
| `Cache-Control` | Req/Resp | Controls CDN and browser caching behavior (e.g., `max-age=86400`). |
| `ETag` | Resp | Entity tag — for simple uploads, this is the MD5 of the object. For multipart uploads, it's `"<md5-of-etags>-<part-count>"` (opaque). |
| `Last-Modified` | Resp | Timestamp of the last modification — used with `If-Modified-Since` for conditional requests. |
| `Accept-Ranges` | Resp | Always `bytes` — indicates S3 supports byte-range fetches. |
| `Range` | Req | Request a specific byte range (e.g., `bytes=0-1048575`). |

### 8.3 ETag Deep Dive

The ETag header deserves special attention because its value differs based on upload method:

| Upload Method | ETag Format | Example |
|---------------|-------------|---------|
| Simple PUT (single request) | MD5 hash of the object data | `"1b2cf535f27731c974343645a3985328"` |
| Multipart upload | MD5 of concatenated part ETags, suffixed with part count | `"4d9320c13b784b09a9932b3c521d36a2-3"` |
| SSE-KMS encrypted | Opaque (not necessarily MD5) | `"a3bf4abc931cc9f60e8e53e04d39b432"` |

**Interview insight:** If someone asks "how do you verify integrity of an S3 object?",
don't say "compare the ETag to the MD5." That only works for simple, non-encrypted uploads.
For multipart uploads or SSE-KMS, use the additional checksum headers
(`x-amz-checksum-sha256`, etc.) instead.

---

## 9. Advanced APIs (Brief)

### 9.1 CopyObject (Server-Side Copy)

Copy an object without downloading it to the client. The copy happens entirely within S3's
infrastructure.

```http
PUT /photos/2024/sunset-backup.jpg HTTP/1.1
Host: backup-bucket.s3.us-east-1.amazonaws.com
x-amz-copy-source: /my-bucket/photos/2024/sunset.jpg
x-amz-metadata-directive: COPY
Authorization: AWS4-HMAC-SHA256 ...
x-amz-date: 20260212T120000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb924...
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<CopyObjectResult>
  <LastModified>2026-02-12T12:00:01.000Z</LastModified>
  <ETag>"1b2cf535f27731c974343645a3985328"</ETag>
</CopyObjectResult>
```

**Use cases:**
- Cross-region replication (with cross-region copy)
- Rename an object (copy to new key, delete old key — S3 has no native rename)
- Change storage class (copy with new `x-amz-storage-class` header)
- Change encryption (copy with new `x-amz-server-side-encryption` header)
- Change metadata (use `x-amz-metadata-directive: REPLACE` and provide new metadata)

**Limitation:** Single CopyObject works for objects up to 5 GB. For larger objects, use
multipart upload with `UploadPartCopy` (copies a byte range from a source object as a part).

### 9.2 SelectObjectContent (S3 Select)

Query CSV, JSON, or Parquet files in-place using SQL-like expressions, without downloading
the entire object.

```http
POST /data/sales-2026.csv?select&select-type=2 HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Type: application/xml
Authorization: AWS4-HMAC-SHA256 ...

<?xml version="1.0" encoding="UTF-8"?>
<SelectObjectContentRequest xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Expression>SELECT s.product, s.revenue FROM S3Object s WHERE s.region = 'us-west-2' AND CAST(s.revenue AS DECIMAL) > 10000</Expression>
  <ExpressionType>SQL</ExpressionType>
  <InputSerialization>
    <CSV>
      <FileHeaderInfo>USE</FileHeaderInfo>
      <FieldDelimiter>,</FieldDelimiter>
    </CSV>
    <CompressionType>GZIP</CompressionType>
  </InputSerialization>
  <OutputSerialization>
    <JSON>
      <RecordDelimiter>\n</RecordDelimiter>
    </JSON>
  </OutputSerialization>
</SelectObjectContentRequest>
```

The response is an event stream with `Records`, `Stats`, and `End` events. This can reduce
data transfer by 80-99% for selective queries on large files.

### 9.3 Object Tagging

Tags are key-value pairs (up to 10 per object) used for lifecycle rules, access control,
analytics, and cost allocation.

```http
PUT /photos/2024/sunset.jpg?tagging HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Type: application/xml
Authorization: AWS4-HMAC-SHA256 ...

<?xml version="1.0" encoding="UTF-8"?>
<Tagging>
  <TagSet>
    <Tag>
      <Key>environment</Key>
      <Value>production</Value>
    </Tag>
    <Tag>
      <Key>team</Key>
      <Value>data-engineering</Value>
    </Tag>
  </TagSet>
</Tagging>
```

```http
GET /photos/2024/sunset.jpg?tagging HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Authorization: AWS4-HMAC-SHA256 ...
```

```http
HTTP/1.1 200 OK
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<Tagging>
  <TagSet>
    <Tag>
      <Key>environment</Key>
      <Value>production</Value>
    </Tag>
    <Tag>
      <Key>team</Key>
      <Value>data-engineering</Value>
    </Tag>
  </TagSet>
</Tagging>
```

### 9.4 Bucket Notification Configuration

Configure S3 to send events to SNS, SQS, or Lambda when objects are created, deleted, etc.

```http
PUT /?notification HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Type: application/xml
Authorization: AWS4-HMAC-SHA256 ...

<?xml version="1.0" encoding="UTF-8"?>
<NotificationConfiguration>
  <LambdaFunctionConfiguration>
    <Id>image-resize-trigger</Id>
    <LambdaFunctionArn>arn:aws:lambda:us-east-1:123456789012:function:resize-image</LambdaFunctionArn>
    <Event>s3:ObjectCreated:*</Event>
    <Filter>
      <S3Key>
        <FilterRule>
          <Name>prefix</Name>
          <Value>uploads/images/</Value>
        </FilterRule>
        <FilterRule>
          <Name>suffix</Name>
          <Value>.jpg</Value>
        </FilterRule>
      </S3Key>
    </Filter>
  </LambdaFunctionConfiguration>
  <QueueConfiguration>
    <Id>log-processing-queue</Id>
    <Queue>arn:aws:sqs:us-east-1:123456789012:log-processor</Queue>
    <Event>s3:ObjectCreated:Put</Event>
    <Filter>
      <S3Key>
        <FilterRule>
          <Name>prefix</Name>
          <Value>logs/</Value>
        </FilterRule>
      </S3Key>
    </Filter>
  </QueueConfiguration>
</NotificationConfiguration>
```

**Supported events include:**
- `s3:ObjectCreated:*` (Put, Post, Copy, CompleteMultipartUpload)
- `s3:ObjectRemoved:*` (Delete, DeleteMarkerCreated)
- `s3:ObjectRestore:*` (initiated, completed — for Glacier restores)
- `s3:LifecycleTransition` (object transitioned to another storage class)

### 9.5 Bucket Lifecycle Configuration

Automate object transitions (storage class changes) and expirations (deletions).

```http
PUT /?lifecycle HTTP/1.1
Host: my-bucket.s3.us-east-1.amazonaws.com
Content-Type: application/xml
Authorization: AWS4-HMAC-SHA256 ...

<?xml version="1.0" encoding="UTF-8"?>
<LifecycleConfiguration>
  <Rule>
    <ID>move-old-logs-to-glacier</ID>
    <Filter>
      <Prefix>logs/</Prefix>
    </Filter>
    <Status>Enabled</Status>
    <Transition>
      <Days>30</Days>
      <StorageClass>STANDARD_IA</StorageClass>
    </Transition>
    <Transition>
      <Days>90</Days>
      <StorageClass>GLACIER</StorageClass>
    </Transition>
    <Expiration>
      <Days>365</Days>
    </Expiration>
  </Rule>
  <Rule>
    <ID>abort-incomplete-multipart</ID>
    <Filter>
      <Prefix></Prefix>
    </Filter>
    <Status>Enabled</Status>
    <AbortIncompleteMultipartUpload>
      <DaysAfterInitiation>7</DaysAfterInitiation>
    </AbortIncompleteMultipartUpload>
  </Rule>
</LifecycleConfiguration>
```

---

## 10. Rate Limits & Throttling

### 10.1 Per-Prefix Scaling

S3 automatically partitions data by key prefix to handle high request rates. The commonly
cited numbers (which are now automatically scaled) are:

| Operation Type | Baseline Rate (per prefix) |
|---------------|---------------------------|
| PUT/POST/DELETE | 3,500 requests/second |
| GET/HEAD | 5,500 requests/second |

**Since 2018**, S3 automatically scales these limits. If your workload consistently exceeds
these rates on a given prefix, S3 transparently re-partitions the index to handle the load.
You do not need to pre-provision or request limit increases.

### 10.2 How S3 Partitions Keys

```
Keys in bucket:
  logs/2026/02/12/server1/access.log
  logs/2026/02/12/server2/access.log
  logs/2026/02/12/server3/access.log
  ...

S3 internally partitions the key space. If all writes hammer
the prefix "logs/2026/02/12/", that single partition becomes a hot spot.

S3 detects this and automatically splits the partition:
  Partition A: logs/2026/02/12/server1/ ... logs/2026/02/12/server4/
  Partition B: logs/2026/02/12/server5/ ... logs/2026/02/12/server9/

Each partition now independently handles 3,500 PUT/s + 5,500 GET/s.
```

### 10.3 503 SlowDown Response

When S3 throttles you:

```http
HTTP/1.1 503 Slow Down
Content-Type: application/xml

<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>SlowDown</Code>
  <Message>Please reduce your request rate.</Message>
  <RequestId>4442587FB7D0A2F9</RequestId>
  <HostId>vlR7PnpV2Ce81l0PRw6jlUpck7aLJmLR5lpF+IEKAN0=</HostId>
</Error>
```

### 10.4 Best Practices for High-Throughput Workloads

| Practice | Explanation |
|----------|-------------|
| **Spread keys across prefixes** | Instead of `logs/{timestamp}`, use `logs/{random-prefix}/{timestamp}` or `logs/{hash(timestamp)}/{timestamp}` to distribute across partitions |
| **Exponential backoff with jitter** | On 503, don't retry immediately — use the backoff algorithm from Section 7.3 |
| **Use byte-range GETs for parallelism** | Instead of one GET for a 10 GB file, issue 100 parallel range requests for 100 MB each |
| **Use multipart upload parallelism** | Upload parts in parallel (e.g., 10 concurrent part uploads) to maximize throughput |
| **Use S3 Transfer Acceleration** | For cross-region uploads, use the accelerated endpoint to route through CloudFront edge locations |
| **Use ListObjectsV2 pagination** | Don't try to list millions of objects in one call — use `max-keys` and continuation tokens |
| **Enable S3 request metrics** | CloudWatch metrics for 4xx/5xx rates help identify throttling before it becomes critical |

### 10.5 Per-Account Limits (Not Commonly Hit)

| Limit | Value |
|-------|-------|
| Buckets per account | 100 (soft limit — can be raised to 1,000 via support request) |
| Object key length | 1,024 bytes (UTF-8 encoded) |
| Object metadata (user-defined) | 2 KB total across all `x-amz-meta-*` headers |
| Tags per object | 10 |
| Tag key length | 128 characters |
| Tag value length | 256 characters |

---

## 11. Design Decisions Summary

| Decision | S3's Choice | Alternative | Why S3 Chose This |
|----------|-------------|-------------|-------------------|
| **Protocol** | REST/HTTP | gRPC, custom TCP | Browser compatibility, CDN integration, universality. Every device speaks HTTP. |
| **URL style** | Virtual-hosted (`bucket.s3.amazonaws.com`) | Path-style (`s3.amazonaws.com/bucket`) | DNS-based routing enables per-bucket load balancing and CDN caching. |
| **Namespace** | Flat (key-value, `/` is just a character) | Hierarchical (filesystem with directories) | Simpler metadata index, no directory rename storms, O(1) key lookup vs O(n) directory traversal. |
| **Pagination** | Continuation token | Offset-based (`?offset=1000`) | Consistent pagination under concurrent writes. Tokens encode index position, not row count. |
| **Consistency** | Strong read-after-write (since Dec 2020) | Eventual consistency | Eliminates "read-your-writes" bugs. Required read-path replication of metadata witness. |
| **Integrity** | Content-MD5 + additional checksums (SHA-256, CRC32C) | Trust the network | Defense in depth. Detects bit-rot, network corruption, and software bugs. Multiple checksum options balance speed vs strength. |
| **Authentication** | SigV4 (request signing) | Bearer tokens, API keys, mTLS | No credential transmission (credentials never cross the wire). Replay protection via timestamps. Per-request scope (region, service, date). |
| **Encryption** | SSE-S3 (default since Jan 2023), SSE-KMS, SSE-C | Client-side only | Defense in depth. SSE-S3 as default means every object is encrypted at rest with zero customer effort. SSE-KMS for regulatory requirements. |
| **Versioning** | Optional, per-bucket, once-enabled-never-fully-disabled | Always-on or no versioning | Flexibility — not all workloads need versioning. "Cannot disable" prevents accidental data loss after enabling. |
| **Delete with versioning** | Soft delete (delete marker) | Hard delete | Enables point-in-time recovery. Delete markers are cheap (zero-byte). Permanent delete requires explicit version ID. |
| **Multipart upload** | Client-driven (client decides part boundaries, parallelism) | Server-driven (server splits the stream) | Client controls parallelism level, retry granularity, and part size. Failed parts can be individually retried without re-uploading the whole object. |
| **Object size** | 5 TB max | Unlimited | 10,000 parts x 5 GB practical max. Larger objects use data lake patterns (partition into multiple objects). |
| **List API** | XML response with prefix/delimiter | JSON, streaming, or filesystem-style ls | XML was standard in 2006 (S3's launch year). Prefix/delimiter enables filesystem simulation without actual directory overhead. |
| **Storage classes** | 7 classes (Standard through Deep Archive) | Single tier | Different durability/availability/cost tradeoffs for different access patterns. Lifecycle rules automate transitions. |
| **Error format** | XML with S3-specific error codes | JSON, plain text | Consistency with the XML API surface. Error codes (NoSuchKey, SlowDown) are more precise than HTTP status codes alone. |
| **Presigned URLs** | Query-string SigV4 | Separate token service | No additional infrastructure — the same signing mechanism that authenticates API calls also generates presigned URLs. |

---

## Quick Reference: Common Interview Questions

**Q: Why does S3 use REST instead of gRPC?**
A: Universal client compatibility (browsers, curl, CDNs all speak HTTP natively). gRPC would
optimize throughput for server-to-server but exclude the long tail of consumers.

**Q: How does S3 simulate directories?**
A: Flat namespace with prefix/delimiter on LIST. Delimiter `/` groups keys with common
prefixes into `CommonPrefixes` entries, simulating subdirectory listing.

**Q: What happens if a client retries a PUT that already succeeded?**
A: PUT is idempotent. The same content overwrites the same key — no duplication, no
corruption. With versioning enabled, each PUT creates a new version, but the client sees
the same result.

**Q: How does S3 handle integrity?**
A: Content-MD5 (legacy) + additional checksums (SHA-256, CRC32C). S3 verifies on write and
returns `400 BadDigest` on mismatch. ETag is returned for client-side verification.

**Q: How does pagination work without skipping or duplicating objects?**
A: Continuation tokens encode the position in the sorted key index. Unlike offset-based
pagination, concurrent writes between pages don't cause skips or duplicates.

**Q: How does authentication work?**
A: SigV4 — the client signs the request using a derived key. Credentials never cross the
wire. The server independently computes the same signature. Clock skew tolerance: 15 minutes.

---

## Footer — Companion Documents

This document is part of a system design deep-dive series:

- [Interview Simulation](interview-simulation.md) — Full 45-minute mock interview walkthrough for Amazon S3
- [Data Flow & Storage Internals](data-flow-internals.md) — How S3 stores, replicates, and retrieves data internally
- [Consistency & Replication](consistency-replication.md) — Strong consistency implementation, witness protocol, quorum reads
- [Security & Access Control](security-access-control.md) — IAM policies, bucket policies, ACLs, encryption, VPC endpoints
- [Scalability & Performance](scalability-performance.md) — Partitioning, auto-scaling, Transfer Acceleration, caching patterns
