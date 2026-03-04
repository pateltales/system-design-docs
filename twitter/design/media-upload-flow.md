# Twitter System Design — Media Upload Flow (Deep Dive)

> The interviewer asked: "How does `POST /v1/media/upload/init` work? Why does the user upload media before the tweet post call?"

---

## The Core Question

Why is media upload a **separate, two-step process** that happens **before** the tweet is posted?

The answer comes down to: **decoupling large binary uploads from lightweight JSON API calls.**

---

## The Problem with Single-Call Upload

Imagine the naive approach — a single `POST /v1/tweets` that includes the image/video as a multipart form upload:

```
POST /v1/tweets
Content-Type: multipart/form-data

--boundary
Content-Disposition: form-data; name="content"
Hello world!
--boundary
Content-Disposition: form-data; name="image"; filename="photo.jpg"
Content-Type: image/jpeg

<... 5 MB of binary data ...>
--boundary--
```

**Why this is terrible at scale:**

| Problem | Impact |
|---------|--------|
| **App server bandwidth** | Each Tweet Service instance is now proxying 5MB+ of binary data through itself to S3. At 6K tweets/sec, even if 10% have media, that's 600 × 5MB = **3 GB/sec** of upload traffic flowing through your API servers. |
| **Long-lived connections** | Uploading a 5MB image on a slow mobile connection could take 10-30 seconds. Your API server thread/connection is held open the entire time, reducing capacity. |
| **Timeout risk** | If the upload takes too long, the request times out. The user retries. Now you have a partially-uploaded image and a failed tweet — messy cleanup. |
| **No retry granularity** | If the tweet text validation fails (too long, spam detected), the user already wasted time uploading the image. If the image is corrupt, the user already composed the tweet for nothing. |
| **Video is worse** | Videos can be 100MB-512MB. Streaming that through your API server is a non-starter. |

---

## The Solution: Pre-signed URL Upload (Two-Phase)

We split the process into two independent phases:

```
Phase 1: Upload media directly to S3 (bypass our servers entirely)
Phase 2: Post tweet with a lightweight reference (media_id) to the already-uploaded media
```

### Complete Flow Diagram

```
┌────────────┐                  ┌───────────────┐                ┌─────────────┐
│   Client   │                  │ Media Service  │                │     S3      │
│ (iOS App)  │                  │ (our server)   │                │ (storage)   │
└─────┬──────┘                  └───────┬───────┘                └──────┬──────┘
      │                                 │                               │
      │  ① POST /v1/media/upload/init   │                               │
      │  { media_type: "image/jpeg",    │                               │
      │    file_size: 2048576 }         │                               │
      │ ───────────────────────────────▶│                               │
      │                                 │                               │
      │                                 │  Validate request:            │
      │                                 │  - Is file type allowed?      │
      │                                 │  - Is file size within limit? │
      │                                 │  - Is user rate-limited?      │
      │                                 │                               │
      │                                 │  Generate media_id            │
      │                                 │  Create DB record (status=    │
      │                                 │    'pending')                 │
      │                                 │                               │
      │                                 │  Generate pre-signed S3 URL   │
      │                                 │  (PUT permission, 30min TTL)  │
      │                                 │                               │
      │  ② 200 OK                       │                               │
      │  { media_id: "med_8a7f3b2c",   │                               │
      │    upload_url: "https://s3...", │                               │
      │    expires_at: "..." }          │                               │
      │ ◀───────────────────────────────│                               │
      │                                 │                               │
      │  ③ PUT https://s3.amazonaws.com/twitter-media/.../med_8a7f3b2c │
      │     Content-Type: image/jpeg                                    │
      │     <binary image data - 2MB>                                   │
      │ ───────────────────────────────────────────────────────────────▶│
      │                                                                 │
      │                                 │   S3 Event Notification       │
      │                                 │ ◀─────────────────────────────│
      │                                 │                               │
      │  ④ 200 OK (from S3)             │  ⑤ Async processing:         │
      │ ◀───────────────────────────────────────────────────────────────│
      │                                 │  - Validate file integrity    │
      │                                 │  - Content moderation (NSFW)  │
      │                                 │  - Generate thumbnails        │
      │                                 │  - Transcode video (if video) │
      │                                 │  - Update DB: status='ready', │
      │                                 │    cdn_url='https://cdn...'   │
      │                                 │                               │
      │                                 │                               │
      │  ⑥ POST /v1/tweets              │                               │
      │  { content: "Hello!",           │                               │
      │    media_ids: ["med_8a7f3b2c"]} │                               │
      │ ───────────────────────────────▶│                               │
      │                                 │                               │
      │                                 │  Check: Is med_8a7f3b2c       │
      │                                 │  status == 'ready'?           │
      │                                 │  - YES → create tweet         │
      │                                 │  - NO  → 400 MEDIA_NOT_READY │
      │                                 │                               │
      │  ⑦ 201 Created                  │                               │
      │  { tweet with media embedded }  │                               │
      │ ◀───────────────────────────────│                               │
      │                                 │                               │
```

---

## Step-by-Step Breakdown

### Step ①: Client calls `POST /v1/media/upload/init`

The client tells our server: "I want to upload a JPEG that's 2MB."

```http
POST /v1/media/upload/init HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

{
  "media_type": "image/jpeg",
  "file_size_bytes": 2048576,
  "filename": "photo.jpg"
}
```

**What the server does:**

```python
def init_upload(request):
    # 1. Validate
    if request.media_type not in ALLOWED_TYPES:
        return 400, "UNSUPPORTED_MEDIA_TYPE"
    if request.file_size > MAX_SIZE[request.media_type]:
        return 400, "FILE_TOO_LARGE"
    if rate_limiter.is_exceeded(request.user_id, "media_upload"):
        return 429, "RATE_LIMIT_EXCEEDED"

    # 2. Generate a unique media_id
    media_id = generate_media_id()  # e.g., "med_8a7f3b2c"

    # 3. Determine the S3 key (where the file will live)
    s3_key = f"images/2026/06/02/{media_id}.jpg"

    # 4. Generate a pre-signed PUT URL
    #    This URL allows the client to upload directly to S3
    #    without needing AWS credentials
    upload_url = s3_client.generate_presigned_url(
        'put_object',
        Params={
            'Bucket': 'twitter-media-us-east-1',
            'Key': s3_key,
            'ContentType': 'image/jpeg',
            'ContentLength': request.file_size_bytes,
        },
        ExpiresIn=1800,  # 30 minutes
    )

    # 5. Create a database record tracking this upload
    db.execute("""
        INSERT INTO media (media_id, user_id, media_type, s3_key,
                          content_type, file_size_bytes, status)
        VALUES (?, ?, 'image', ?, 'image/jpeg', ?, 'pending')
    """, media_id, request.user_id, s3_key, request.file_size_bytes)

    # 6. Return the upload URL to the client
    return 200, {
        "media_id": media_id,
        "upload_url": upload_url,
        "expires_at": now() + 30_minutes
    }
```

**Key insight:** Our server never touches the actual file. It only generates a "permission slip" (pre-signed URL) that says "S3, please accept a PUT of this exact file to this exact location, from anyone who has this URL, within the next 30 minutes."

### Step ②-③: Client uploads directly to S3

The client takes the `upload_url` and does a raw HTTP PUT with the file:

```http
PUT https://twitter-media-us-east-1.s3.amazonaws.com/images/2026/06/02/med_8a7f3b2c.jpg?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=...&X-Amz-Signature=...
Content-Type: image/jpeg
Content-Length: 2048576

<binary image data>
```

**This goes directly from the client's phone → S3.** Our API servers are not in the path. This is the entire point.

### Step ④-⑤: S3 triggers async processing

When the file lands in S3, an [S3 Event Notification](https://docs.aws.amazon.com/AmazonS3/latest/userguide/NotificationHowTo.html) fires, which triggers a processing pipeline:

```
S3 PUT Event → SQS Queue → Media Processing Workers (or Lambda)
```

The processing pipeline:
1. **Validates** the file (is it actually a JPEG? Is it corrupt?)
2. **Content moderation** (NSFW detection via ML model — e.g., Amazon Rekognition)
3. **Generates thumbnails** (150x150, 300x300, 600x600)
4. **For videos:** transcodes to multiple resolutions (360p, 720p, 1080p)
5. **Generates CDN URL** (CloudFront distribution URL)
6. **Updates the DB record:**

```sql
UPDATE media
SET status = 'ready',
    cdn_url = 'https://media.twitter.com/img/med_8a7f3b2c.jpg',
    width = 1200,
    height = 800
WHERE media_id = 'med_8a7f3b2c';
```

### Step ⑥: Client posts the tweet (referencing the media_id)

Now the client creates the tweet — this is a tiny, fast JSON request:

```http
POST /v1/tweets HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json

{
  "content": "Check out this sunset! 🌅",
  "media_ids": ["med_8a7f3b2c"]
}
```

**What Tweet Service does with media_ids:**

```python
def create_tweet(request):
    # Validate media_ids
    for media_id in request.media_ids:
        media = db.get_media(media_id)

        if media is None:
            return 400, "INVALID_MEDIA_ID"

        if media.user_id != request.user_id:
            return 403, "MEDIA_NOT_OWNED"  # Can't use someone else's upload

        if media.status == 'pending':
            return 400, "MEDIA_NOT_READY"  # Still processing

        if media.status == 'failed':
            return 400, "MEDIA_PROCESSING_FAILED"

        if media.status != 'ready':
            return 400, "INVALID_MEDIA_STATE"

    # Proceed with normal tweet creation...
    tweet = create_tweet_in_db(
        content=request.content,
        media_urls=[db.get_media(mid).cdn_url for mid in request.media_ids]
    )
    return 201, tweet
```

---

## Why This Ordering? (Upload First, Tweet Second)

### The Timeline From User's Perspective

```
User opens compose screen
       │
       ▼
User taps "attach photo" → selects photo from gallery
       │
       ▼
Client immediately calls POST /v1/media/upload/init ──────┐
       │                                                    │
       ▼                                                    ▼
User is still typing their tweet text...          Upload happening in
       │                                          background (S3 direct)
       ▼                                                    │
User finishes typing, taps "Post"                          │
       │                                                    ▼
       │                                          Upload complete,
       │                                          media_id = "med_8a7f3b2c"
       ▼
Client calls POST /v1/tweets
  { content: "...", media_ids: ["med_8a7f3b2c"] }
       │
       ▼
Tweet posted instantly (tiny JSON payload)
```

**The UX win:** While the user is composing their tweet text, the media is already uploading in the background. By the time they tap "Post", the upload is done and the tweet creation is instant. If we bundled them together, the user would tap "Post" and then wait 5-30 seconds for the image to upload.

### What If the Upload Isn't Done Yet?

Two strategies:

**Option A: Client waits (simple)**
```
Client taps "Post"
  → Check: is media upload complete?
    → YES: POST /v1/tweets immediately
    → NO:  Show progress bar, wait for upload, then POST /v1/tweets
```

**Option B: Client polls media status (robust)**
```
GET /v1/media/{media_id}/status

Response:
{
  "media_id": "med_8a7f3b2c",
  "status": "processing",    // pending → processing → ready | failed
  "progress_percent": 75
}
```

The client can poll this every 1-2 seconds until status = "ready", then post the tweet.

---

## What About Large Videos? (Chunked Upload)

For large files (> 10MB), we use **chunked/resumable upload** instead of a single PUT:

```
┌──────────┐                    ┌───────────────┐              ┌─────┐
│  Client   │                    │ Media Service  │              │ S3  │
└─────┬────┘                    └───────┬───────┘              └──┬──┘
      │                                 │                         │
      │ ① POST /v1/media/upload/init    │                         │
      │   { media_type: "video/mp4",    │                         │
      │     file_size: 104857600 }      │  (100MB video)          │
      │ ───────────────────────────────▶│                         │
      │                                 │  Initiate S3 multipart  │
      │                                 │  upload                  │
      │                                 │ ───────────────────────▶│
      │                                 │  ◀── upload_id           │
      │                                 │                         │
      │ ② 200 OK                        │                         │
      │   { media_id, upload_id,        │                         │
      │     chunk_size: 5242880,        │  (5MB chunks)           │
      │     num_chunks: 20,             │                         │
      │     chunk_upload_urls: [        │                         │
      │       { part: 1, url: "..." },  │                         │
      │       { part: 2, url: "..." },  │                         │
      │       ...                       │                         │
      │     ] }                         │                         │
      │ ◀───────────────────────────────│                         │
      │                                 │                         │
      │ ③ PUT chunk 1 (5MB) ──────────────────────────────────▶│
      │    PUT chunk 2 (5MB) ──────────────────────────────────▶│
      │    PUT chunk 3 (5MB) ──────────────────────────────────▶│
      │    ... (parallel, 3-4 chunks at a time)                  │
      │    PUT chunk 20 (5MB) ─────────────────────────────────▶│
      │                                 │                         │
      │ ④ POST /v1/media/upload/complete│                         │
      │   { media_id, upload_id }       │                         │
      │ ───────────────────────────────▶│                         │
      │                                 │  Complete multipart     │
      │                                 │  upload in S3           │
      │                                 │ ───────────────────────▶│
      │                                 │                         │
      │ ⑤ 200 OK { status: processing } │                         │
      │ ◀───────────────────────────────│                         │
      │                                 │  Async: transcode,      │
      │                                 │  thumbnail, moderate... │
```

**Why chunked?**
- **Resumable:** If the connection drops after uploading 15 of 20 chunks, only re-upload the remaining 5. Don't restart from scratch.
- **Parallel:** Upload 3-4 chunks concurrently for faster throughput.
- **Progress tracking:** Client knows exactly what percentage is uploaded (chunk 15/20 = 75%).

---

## What About Unused Media?

If a user uploads media but never posts the tweet (closes the app, changes their mind):

```
Cleanup job (runs hourly):
  SELECT media_id FROM media
  WHERE status IN ('pending', 'ready')
    AND created_at < NOW() - INTERVAL 24 HOURS
    AND media_id NOT IN (SELECT DISTINCT media_id FROM tweet_media);

  → Delete from S3
  → Delete from media table
```

We keep orphaned media for 24 hours (in case the user comes back to finish composing), then garbage collect.

---

## Pre-signed URL Security

**Q: Isn't it insecure to give the client a direct S3 URL?**

No. The pre-signed URL is:
- **Time-limited:** Expires after 30 minutes.
- **Operation-specific:** Only allows `PUT` (upload), not `GET` (download) or `DELETE`.
- **Path-specific:** Only allows upload to the exact S3 key we specified.
- **Size-constrained:** Only accepts files up to the declared `Content-Length`.
- **Content-type locked:** Only accepts the declared `Content-Type`.
- **Signed:** The URL includes an HMAC signature derived from our AWS secret key. Tampering with any parameter invalidates the signature.

```
https://twitter-media-us-east-1.s3.amazonaws.com/images/2026/06/02/med_8a7f3b2c.jpg
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=AKIAIOSFODNN7EXAMPLE/20260602/us-east-1/s3/aws4_request
  &X-Amz-Date=20260602T220530Z
  &X-Amz-Expires=1800
  &X-Amz-SignedHeaders=content-length;content-type;host
  &X-Amz-Signature=fe5f80f77d5fa3beca038a248ff027d0445342fe2855ddc963176630326f1024
```

If someone intercepts this URL, they can only upload one file to one specific location within 30 minutes. That's acceptable risk.

---

## Summary: Why Upload-First Works

| Benefit | Explanation |
|---------|-------------|
| **Zero binary traffic through API servers** | Clients upload directly to S3. API servers only handle tiny JSON. |
| **Background upload while composing** | User experience: attach photo → start uploading in background → keep typing → tap Post → instant. |
| **Independent retry** | Upload failed? Retry just the upload. Tweet validation failed? No wasted upload time. |
| **Chunked/resumable for large files** | Videos can be uploaded in parallel chunks with resume-on-failure. |
| **Processing happens before tweet creation** | By the time the user posts, the image is already validated, moderated, thumbnailed, and CDN-ready. |
| **Decoupled scaling** | Media upload infra (S3, processing workers) scales independently from tweet posting infra. |

---

*This document complements the [API contracts](api-contracts.md) and [datastore design](datastore-design.md).*