# WhatsApp — Media Upload, Storage & Delivery Deep Dive

> Companion deep dive to the [interview simulation](01-interview-simulation.md). This document explores how a WhatsApp-like chat application handles media (images, videos, audio, documents) — from client-side encryption and upload, through server-side storage, to recipient download and decryption.

---

## Table of Contents

1.  [Why Media is Architecturally Different from Text](#1-why-media-is-architecturally-different-from-text)
2.  [End-to-End Encrypted Media Upload Flow](#2-end-to-end-encrypted-media-upload-flow)
3.  [Chunked Resumable Uploads](#3-chunked-resumable-uploads)
4.  [Client-Side Media Compression](#4-client-side-media-compression)
5.  [Thumbnail Generation and Blurred Previews](#5-thumbnail-generation-and-blurred-previews)
6.  [Media Storage Architecture](#6-media-storage-architecture)
7.  [Media Download and Decryption Flow](#7-media-download-and-decryption-flow)
8.  [Forwarding and Deduplication](#8-forwarding-and-deduplication)
9.  [Scale Numbers and Back-of-Envelope Math](#9-scale-numbers-and-back-of-envelope-math)
10. [End-to-End Encrypted Media — Security Analysis](#10-end-to-end-encrypted-media--security-analysis)
11. [Contrast with Telegram](#11-contrast-with-telegram)
12. [Contrast with Discord](#12-contrast-with-discord)

---

## 1. Why Media is Architecturally Different from Text

Text messages in WhatsApp are tiny — typically under 1 KB encrypted. Media items
are 3-4 orders of magnitude larger: a compressed image is 100-300 KB, a video can
be 2-10 MB, and a document can reach the platform's size limit (historically 16 MB
on WhatsApp, increased to 2 GB for documents in 2022 [UNVERIFIED — check official
release notes]).

This size difference creates a fundamental architectural split:

```
Text message path:
  Sender → [WebSocket] → Chat Server → [WebSocket] → Recipient
  Size: ~1 KB. Latency-critical. Delivered in-band via persistent connection.

Media path:
  Sender → [HTTPS upload] → Media Server → [Object Store]
  Recipient ← [HTTPS download] ← CDN ← [Object Store]
  Size: 100 KB - 2 GB. Throughput-critical. Delivered out-of-band via HTTP.
```

If you tried to send a 5 MB video through the WebSocket message pipeline, you
would block the connection for other messages, overwhelm the chat server's memory,
and create massive write amplification in the message store (especially for group
fan-out — 1024 copies of a 5 MB blob).

**The key insight**: media content and media metadata travel on completely
separate paths. The encrypted blob goes to the media server. The reference to
that blob (mediaId, encryption key, thumbnail) goes through the normal message
pipeline.

---

## 2. End-to-End Encrypted Media Upload Flow

### 2.1 The Full Upload Sequence

```
 SENDER                    MEDIA SERVER              CHAT SERVER              RECIPIENT
   |                           |                         |                       |
   |  1. Generate random       |                         |                       |
   |     AES-256 key (K)       |                         |                       |
   |     + random IV           |                         |                       |
   |                           |                         |                       |
   |  2. Compress media        |                         |                       |
   |     (client-side)         |                         |                       |
   |                           |                         |                       |
   |  3. Encrypt compressed    |                         |                       |
   |     media with K          |                         |                       |
   |     (AES-256-CBC)         |                         |                       |
   |                           |                         |                       |
   |  4. Compute SHA-256       |                         |                       |
   |     of encrypted blob     |                         |                       |
   |                           |                         |                       |
   |  5. Generate thumbnail    |                         |                       |
   |     (low-res, blurred)    |                         |                       |
   |                           |                         |                       |
   |  6. Upload encrypted blob |                         |                       |
   |---[HTTPS POST]----------->|                         |                       |
   |                           |  7. Store blob in       |                       |
   |                           |     object storage      |                       |
   |                           |                         |                       |
   |  8. Receive mediaId       |                         |                       |
   |<---[200 OK + mediaId]-----|                         |                       |
   |                           |                         |                       |
   |  9. Send E2E encrypted message:                     |                       |
   |     { mediaId, K, IV, SHA-256,                      |                       |
   |       mimeType, fileSize,                           |                       |
   |       thumbnail (base64) }                          |                       |
   |---[WebSocket]---------------------------------->----|                       |
   |                           |                         |  10. Route to         |
   |                           |                         |      recipient        |
   |                           |                         |---[WebSocket]-------->|
   |                           |                         |                       |
   |                           |                         |       11. Show thumb- |
   |                           |                         |           nail to user|
   |                           |                         |                       |
   |                           |         12. Download encrypted blob             |
   |                           |<---[HTTPS GET /media/{mediaId}]----------------|
   |                           |                         |                       |
   |                           |---[encrypted blob]----------------------------->|
   |                           |                         |                       |
   |                           |                         |       13. Verify      |
   |                           |                         |           SHA-256     |
   |                           |                         |                       |
   |                           |                         |       14. Decrypt     |
   |                           |                         |           with K + IV |
   |                           |                         |                       |
   |                           |                         |       15. Display     |
   |                           |                         |           full media  |
```

### 2.2 Step-by-Step Breakdown

**Step 1-2: Key generation and compression.** The sender generates a fresh,
random AES-256 key and initialization vector (IV) for this specific media item.
This key is independent of the Signal Protocol session keys used for text
messages. The media is then compressed client-side (details in Section 4).

**Step 3-4: Encryption and integrity hash.** The compressed media is encrypted
using AES-256-CBC (or AES-256-GCM, which additionally provides authenticated
encryption). A SHA-256 hash of the encrypted blob is computed — this serves as
an integrity check so the recipient can verify the download was not corrupted or
tampered with.

According to the WhatsApp encryption whitepaper, the media encryption uses
AES-256-CBC with HMAC-SHA256 for authentication, and the SHA-256 hash of the
ciphertext is included in the message so the recipient can verify integrity
before decryption.

**Step 5: Thumbnail generation.** A small, low-resolution thumbnail is generated
on the client (see Section 5). This thumbnail is small enough (a few KB) to
include directly in the E2E encrypted message.

**Step 6-8: Upload.** The encrypted blob is uploaded to the media server over
HTTPS. The server does not need any encryption keys — it stores the blob opaquely.
It returns a `mediaId` (a unique identifier, likely content-addressed or a UUID).

**Step 9-10: Message with media reference.** The sender constructs a message
containing the media metadata and encrypts it with the Signal Protocol session
key (the normal E2E encryption for messages). This message travels through the
chat server to the recipient. The critical point: the AES-256 key (K) for the
media is embedded inside the E2E encrypted message. The chat server cannot read
this key.

**Step 11-15: Recipient download and decryption.** The recipient first sees the
blurred thumbnail (instant). In the background, the client downloads the
encrypted blob from the media server (or CDN), verifies the SHA-256 hash,
decrypts with K and IV, and displays the full media.

### 2.3 Why a Separate Key Per Media Item?

Each media item gets its own random AES-256 key rather than reusing the Signal
Protocol session key. This provides:

- **Independent security**: Compromise of one media key does not affect other
  media or text messages.
- **Forwarding support**: When media is forwarded, the same mediaId and key can
  be shared with a new recipient without re-uploading (see Section 8).
- **Key separation**: The Signal Protocol ratchet manages session keys for
  messages. Media keys are orthogonal — no coupling between the two systems.

---

## 3. Chunked Resumable Uploads

### 3.1 The Problem with Single-Shot Uploads

Mobile networks are unreliable. Users switch between Wi-Fi and cellular, walk
through dead zones, enter elevators, and ride subways. A single-shot HTTP upload
of a 10 MB video that fails at 90% means the user must restart from zero. On a
slow 3G connection (~1 Mbps), uploading 10 MB takes ~80 seconds — plenty of time
for a network interruption.

### 3.2 Chunked Upload Protocol

```
CLIENT                              MEDIA SERVER
  |                                      |
  |  POST /media/upload/init             |
  |  { fileSize, mimeType, sha256 }      |
  |------------------------------------->|
  |                                      |  Allocate uploadId
  |  200 OK { uploadId, chunkSize }      |  chunkSize = 256 KB
  |<-------------------------------------|
  |                                      |
  |  PUT /media/upload/{uploadId}/0      |
  |  [chunk 0: bytes 0-262143]           |
  |------------------------------------->|
  |  200 OK { nextChunk: 1 }             |
  |<-------------------------------------|
  |                                      |
  |  PUT /media/upload/{uploadId}/1      |
  |  [chunk 1: bytes 262144-524287]      |
  |------------------------------------->|
  |  200 OK { nextChunk: 2 }             |
  |<-------------------------------------|
  |                                      |
  |       ~~~ network failure ~~~        |
  |                                      |
  |  GET /media/upload/{uploadId}/status |
  |------------------------------------->|
  |  200 OK { completedChunks: [0,1],    |
  |           nextChunk: 2 }             |
  |<-------------------------------------|
  |                                      |
  |  PUT /media/upload/{uploadId}/2      |  Resume from chunk 2
  |  [chunk 2: bytes 524288-786431]      |
  |------------------------------------->|
  |  200 OK { nextChunk: 3 }             |
  |<-------------------------------------|
  |                                      |
  |       ... continues ...              |
  |                                      |
  |  PUT /media/upload/{uploadId}/N      |  Final chunk
  |  [chunk N: remaining bytes]          |
  |------------------------------------->|
  |                                      |  Reassemble + verify SHA-256
  |  200 OK { mediaId }                  |
  |<-------------------------------------|
```

### 3.3 Design Decisions

**Chunk size: 256 KB.** This is a balance between overhead and resumability:

```
Chunk size trade-offs:
  Too small (e.g., 16 KB):
    - Excessive HTTP overhead (headers per chunk)
    - More round trips, higher total latency
    - More server-side bookkeeping

  Too large (e.g., 4 MB):
    - Failure wastes more progress (up to 4 MB lost per retry)
    - Longer time per chunk = higher chance of failure mid-chunk

  256 KB sweet spot:
    - On 3G (~1 Mbps): ~2 seconds per chunk — manageable failure window
    - On 4G (~10 Mbps): ~0.2 seconds per chunk — fast progress
    - On Wi-Fi (~50 Mbps): ~0.04 seconds per chunk — negligible overhead
    - 10 MB video = ~40 chunks — reasonable bookkeeping
```

**Upload ID and server-side state.** The server tracks upload progress (which
chunks are received) keyed by `uploadId`. This state has a TTL (e.g., 24 hours)
— if the user does not complete the upload within that window, the partial
upload is garbage-collected.

**Integrity verification.** The client sends the SHA-256 of the full encrypted
blob at initialization. After the server reassembles all chunks, it computes
SHA-256 and verifies it matches. If not, the upload is rejected — some chunk was
corrupted in transit.

**Parallel chunk upload.** For faster upload on high-bandwidth connections, the
client can upload 2-3 chunks in parallel. The server accepts chunks out of order
and reassembles them by chunk number.

---

## 4. Client-Side Media Compression

### 4.1 Why Compress Before Upload?

```
Without compression:
  Photo from modern phone camera: 5-12 MB (12 MP HEIF/JPEG)
  10-second video clip: 20-50 MB (4K H.265)
  Voice message (1 min): 1-3 MB (uncompressed PCM or high-bitrate AAC)

After WhatsApp compression:
  Photo: 100-300 KB (JPEG, resized to max 1600px)
  10-second video: 1-3 MB (H.264/AAC, lower resolution + bitrate)
  Voice message (1 min): 50-100 KB (Opus, ~16 kbps)
```

Compression happens entirely on the client, before encryption. This is not
optional — it is essential for:

1. **Upload time**: On a 3G connection, uploading 10 MB takes ~80 seconds.
   Uploading 300 KB takes ~2.4 seconds. Users will not wait 80 seconds.
2. **Storage cost**: 6.5 billion media items/day. Even small reductions in
   average size save petabytes.
3. **Download time**: The recipient also benefits — faster download, less data
   usage on metered connections.
4. **Battery**: Transmitting less data uses less radio time, which is one of the
   biggest battery drains on mobile devices.

### 4.2 Compression by Media Type

**Images (JPEG, max 1600px)**

```
Original: 4032 x 3024 pixels, 8 MB HEIF
  |
  v  Resize to fit within 1600 x 1600 bounding box
  |  (maintain aspect ratio)
  v
Resized: 1600 x 1200 pixels
  |
  v  JPEG compression (quality ~70-80%)
  |
  v
Output: ~150-250 KB JPEG

Compression ratio: ~32:1 to ~53:1
```

WhatsApp strips EXIF metadata (GPS location, camera model) for privacy. The
1600px maximum is a conscious product decision — good enough for phone screens,
terrible for printing. This is a chat app, not a photo gallery.

**Videos (H.264/AAC re-encode)**

```
Original: 1080p, H.265, 30fps, 10 Mbps bitrate, 10 sec = ~12.5 MB
  |
  v  Re-encode to H.264 Baseline Profile
  |  (widest device compatibility)
  v
  |  Reduce resolution to 480p or 720p
  |  Reduce bitrate to 1-2 Mbps
  |  Reduce framerate to 30fps (cap)
  v
Output: ~1.2-2.5 MB

Compression ratio: ~5:1 to ~10:1
```

H.264 Baseline Profile is chosen for maximum device compatibility — every phone
from the last 15 years can decode it, including low-end devices with hardware
decoders. Higher-efficiency codecs (H.265, VP9, AV1) would save bandwidth but
risk playback failures on older devices.

**Audio messages (Opus codec, ~16 kbps)**

```
Original voice recording: PCM 16-bit, 16 kHz, mono = ~256 kbps = ~1.9 MB/min
  |
  v  Encode with Opus codec
  |  Target bitrate: ~16 kbps
  |  Sample rate: 16 kHz mono
  v
Output: ~120 KB/min

Compression ratio: ~16:1
```

Opus is the ideal codec for voice messages: it is designed for speech, open and
royalty-free, and achieves excellent quality at very low bitrates. At 16 kbps,
Opus sounds noticeably better than competing codecs (AMR-NB, Speex) at the same
bitrate. A typical 30-second voice message is ~60 KB — small enough to feel
nearly instant.

### 4.3 Compression Happens Before Encryption

This ordering is critical:

```
CORRECT:  Raw media → Compress → Encrypt → Upload
WRONG:    Raw media → Encrypt → Compress → Upload
```

Encrypted data is indistinguishable from random noise — compression algorithms
cannot find patterns in it. Compressing after encryption yields essentially zero
size reduction. You must compress the plaintext, then encrypt the compressed
output.

---

## 5. Thumbnail Generation and Blurred Previews

### 5.1 The User Experience Problem

Media download takes time — seconds on a fast connection, potentially minutes on
a slow one. Without thumbnails, the user sees a generic placeholder ("Loading
image...") with no indication of what the image contains. This is a poor
experience.

WhatsApp's solution: the sender generates a tiny, low-resolution thumbnail and
includes it directly in the E2E encrypted message. The recipient sees the blurred
preview instantly (sub-second, since it arrives with the message), while the full
media downloads in the background. This is WhatsApp's characteristic "blurry
preview" effect.

### 5.2 Thumbnail Generation Process

```
Original image: 1600 x 1200, 200 KB JPEG
  |
  v  Downscale to ~32 x 24 pixels (very low resolution)
  |
  v  Apply blur filter (Gaussian blur, optional — the low resolution
  |  itself creates the blur effect)
  |
  v  JPEG compress at low quality (~30-40%)
  |  OR base64-encode raw pixel data
  v
Thumbnail: ~1-3 KB

This 1-3 KB thumbnail is included in the message payload:
{
  "mediaId": "abc123",
  "encryptionKey": "base64(K)",
  "iv": "base64(IV)",
  "sha256": "hex(hash)",
  "mimeType": "image/jpeg",
  "fileSize": 204800,
  "thumbnail": "base64(thumbnail_bytes)",    <-- ~1-3 KB inline
  "width": 1600,
  "height": 1200
}
```

### 5.3 Why Not Generate Thumbnails Server-Side?

In a non-E2E system (Telegram, Discord, Slack), the server can generate
thumbnails because it has access to the plaintext media. In WhatsApp's E2E model,
the server stores only encrypted blobs — it cannot generate thumbnails because it
cannot decrypt the media.

This is a direct consequence of E2E encryption: any processing that requires
access to plaintext media content (thumbnails, previews, transcription, content
moderation) must happen on the client.

### 5.4 Video Thumbnails

For videos, the client extracts a single frame (typically the first frame or a
frame a few seconds in), applies the same downscale-and-compress process, and
includes it as the thumbnail. The thumbnail serves as the "poster frame" while
the video downloads.

---

## 6. Media Storage Architecture

### 6.1 Storage Tier Design

```
                                    +-----------+
                                    |           |
                                    |    CDN    |  Edge caches for
                                    |  (Global) |  frequently accessed media
                                    |           |
                                    +-----+-----+
                                          |
                                          | Origin pull on cache miss
                                          |
                              +-----------+-----------+
                              |                       |
                              |   Media Origin        |
                              |   (Load Balancer)     |
                              |                       |
                              +---+---------------+---+
                                  |               |
                          +-------+---+   +-------+---+
                          |           |   |           |
                          | Media     |   | Media     |
                          | Server 1  |   | Server N  |
                          |           |   |           |
                          +-----+-----+   +-----+-----+
                                |               |
                                +-------+-------+
                                        |
                              +---------+---------+
                              |                   |
                              |  Object Storage   |
                              |  (S3 / equivalent)|
                              |                   |
                              |  Encrypted blobs  |
                              |  organized by     |
                              |  mediaId          |
                              |                   |
                              +-------------------+
```

### 6.2 Object Storage

Media blobs are stored in object storage (S3 or an equivalent internal system).
Object storage is the right choice because:

- **Scale**: Object stores handle petabytes natively — no filesystem scaling
  issues.
- **Durability**: S3 provides 11 nines (99.999999999%) durability. Media loss
  is unacceptable — once a user sends a photo, they expect it to be downloadable.
- **Cost**: Object storage is cheap per GB ($0.023/GB/month for S3 Standard).
  At petabyte scale, this matters enormously.
- **HTTP-native**: Objects are accessed via HTTP GET/PUT — aligns naturally with
  the upload/download API.

**Blob organization**: Each encrypted blob is stored with `mediaId` as the key.
The `mediaId` is either a UUID or a content-addressed hash (SHA-256 of the
encrypted blob). Content-addressed storage enables natural deduplication for
forwarded media (see Section 8).

### 6.3 CDN for Delivery

Downloads are served through a CDN (Content Delivery Network) rather than
directly from the origin object store:

- **Latency**: CDN edge servers are geographically close to users. A user in
  Mumbai downloads from the Mumbai edge, not from a US data center.
- **Throughput**: CDN absorbs download traffic, preventing the origin from being
  overwhelmed. Popular forwarded media (viral images) would hammer the origin
  without CDN caching.
- **Cost**: CDN egress from edge is cheaper than origin egress at scale.

Since the blobs are encrypted, caching them on CDN edge servers does not create
a privacy risk — the CDN sees only opaque encrypted bytes, not plaintext media.

### 6.4 Retention and TTL

WhatsApp's server-as-transient-relay philosophy extends to media:

```
Media lifecycle:
  1. Sender uploads encrypted blob → stored in object storage
  2. Recipient(s) download the blob
  3. Server may delete the blob after:
     a. All recipients have downloaded it, OR
     b. A retention period expires (e.g., 30 days)

Why delete?
  - Storage cost: 6.5B media/day x 200 KB avg = 1.3 PB/day
  - If retained forever: 1.3 PB/day x 365 = ~475 PB/year
  - With 30-day TTL: steady-state = 1.3 PB/day x 30 = ~39 PB
  - 12x storage reduction from TTL alone
```

[INFERRED — not officially documented] The exact retention policy is not publicly
documented. WhatsApp's FAQ states that media is stored temporarily on their
servers for delivery. The 30-day figure is a reasonable estimate based on the
offline message retention window.

**Contrast with Telegram**: Telegram stores media permanently in the cloud. Users
can access any media ever sent or received from any device. This requires
unbounded storage growth but enables a fundamentally different product experience
(media as a permanent cloud archive).

### 6.5 Storage Classes

For cost optimization, media blobs can be tiered by access pattern:

```
Age 0-2 days:  Hot storage (S3 Standard)     — most downloads happen immediately
Age 2-14 days: Warm storage (S3 Infrequent)  — occasional re-downloads
Age 14-30 days: Cold storage (S3 Glacier)    — rare access, pending deletion
Age 30+ days:  Deleted
```

[INFERRED — not officially documented] WhatsApp has not publicly described their
storage tiering strategy, but any system at this scale would benefit from it.

---

## 7. Media Download and Decryption Flow

### 7.1 Recipient-Side Flow

```
RECIPIENT CLIENT                          CDN / MEDIA SERVER
    |                                           |
    | 1. Receive E2E message with:              |
    |    { mediaId, K, IV, sha256,              |
    |      thumbnail, mimeType }                |
    |                                           |
    | 2. Display thumbnail immediately          |
    |    (blurred preview)                      |
    |                                           |
    | 3. GET /media/{mediaId}                   |
    |------------------------------------------>|
    |                                           |
    | 4. Receive encrypted blob                 |
    |<------------------------------------------|
    |                                           |
    | 5. Compute SHA-256 of received blob       |
    |    Compare with sha256 from message       |
    |    If mismatch → reject (corrupted)       |
    |                                           |
    | 6. Decrypt blob with K and IV             |
    |    AES-256-CBC decryption                 |
    |                                           |
    | 7. Decompress (if applicable)             |
    |                                           |
    | 8. Replace thumbnail with full media      |
    |                                           |
```

### 7.2 Auto-Download Settings

WhatsApp allows users to configure auto-download behavior per network type:

```
Auto-download settings:
  Wi-Fi:     Download images, audio, video, documents automatically
  Cellular:  Download images and audio automatically, not video
  Roaming:   Download nothing automatically

Why this matters:
  - A 5 MB video on cellular costs the user real money in metered markets
  - Users in developing countries (WhatsApp's largest markets: India, Brazil)
    are highly sensitive to data costs
  - This is a product decision with direct infrastructure impact: fewer
    auto-downloads = fewer requests to media servers
```

### 7.3 Retry on Download Failure

If a download fails mid-stream, the client uses HTTP range requests to resume:

```
Initial request:
  GET /media/{mediaId}
  → receives bytes 0 through 524287, then connection drops

Resume request:
  GET /media/{mediaId}
  Range: bytes=524288-
  → receives remaining bytes from offset 524288
```

This is standard HTTP range-based resumption — the media server and CDN support
it natively.

---

## 8. Forwarding and Deduplication

### 8.1 Forward Without Re-Upload

When a user forwards a media message, the media does not need to be re-uploaded:

```
ORIGINAL FLOW:
  Alice → uploads encrypted blob → gets mediaId "abc123"
  Alice → sends message to Bob: { mediaId: "abc123", key: K1, ... }

FORWARD FLOW:
  Bob forwards to Charlie:
  Bob → sends message to Charlie: { mediaId: "abc123", key: K1, ... }
  (No upload needed — the blob is already on the server)

Charlie downloads mediaId "abc123" from the server, decrypts with K1.
```

The server stores one physical copy of the encrypted blob. Multiple messages
reference the same `mediaId`. This is deduplication at the media level.

### 8.2 Why This Works with E2E Encryption

The encryption key (K) is not stored on the server — it is embedded in the E2E
encrypted message. When Bob forwards to Charlie, Bob's client constructs a new
E2E encrypted message (encrypted with Bob-Charlie session key) that contains the
same mediaId and K. The server never learns K, but it does not need to — it just
serves the same encrypted blob to whoever requests it by mediaId.

### 8.3 Security Implication of Forwarding

Anyone who possesses the `mediaId` and encryption key can download and decrypt
the media. The server cannot restrict access based on who was in the original
conversation — it serves the encrypted blob to any authenticated user who
requests the `mediaId`.

This is a trade-off: deduplication and forward-without-re-upload are convenient
and save massive storage, but it means the server cannot enforce access control
at the media level. Access control is enforced at the messaging level — only
users who receive a message with the mediaId and key can access the media.

### 8.4 Deduplication Savings

```
Viral media scenario:
  A popular image is forwarded 1,000,000 times across different conversations.

Without deduplication:
  1,000,000 copies x 200 KB = 200 GB storage

With deduplication (reuse mediaId):
  1 copy x 200 KB = 200 KB storage
  Savings: 99.99998%
```

At WhatsApp's scale, forwarded media is a significant portion of total media
traffic. The "Forwarded" and "Forwarded many times" labels that WhatsApp shows
are metadata signals, but the underlying media blob is shared.

---

## 9. Scale Numbers and Back-of-Envelope Math

### 9.1 Daily Volume

```
Media items shared per day: ~6.5 billion

Breakdown (estimated):
  Images:          ~4.5 billion  (~70%)    Avg size: 150 KB
  Videos:          ~1.0 billion  (~15%)    Avg size: 3 MB
  Audio messages:  ~0.7 billion  (~11%)    Avg size: 80 KB
  Documents:       ~0.3 billion  (~4%)     Avg size: 500 KB
```

[UNVERIFIED — check official sources] The 6.5 billion figure is widely cited but
not consistently verified from a single official WhatsApp source. The breakdown
by type is an estimate.

### 9.2 Daily Storage Throughput

```
Daily new media storage:
  Images:     4.5B x 150 KB  = 675 TB
  Videos:     1.0B x 3 MB    = 3,000 TB = 3 PB
  Audio:      0.7B x 80 KB   = 56 TB
  Documents:  0.3B x 500 KB  = 150 TB
  ─────────────────────────────
  Total:                      ~3.88 PB/day

  Rough estimate: ~1.3 PB/day (using blended average of ~200 KB/item)
  to ~3.9 PB/day (using per-type averages that weight videos more heavily)

  The actual figure depends heavily on the video percentage and average
  video size, which dominate the total despite being only 15% of items.
```

### 9.3 Upload/Download Throughput

```
Upload throughput:
  ~6.5 billion uploads/day
  = 6.5B / 86,400 sec
  = ~75,000 uploads/second average
  Peak (3x): ~225,000 uploads/second

Download throughput:
  Each media is downloaded at least once (1:1 messages)
  Group messages: up to 1024 downloads per media item
  Forwarded media: potentially millions of downloads
  Estimate: average 2-3 downloads per upload
  = ~150,000 - 225,000 downloads/second average

Bandwidth:
  At ~200 KB average media size:
  Upload: 75,000/sec x 200 KB = ~15 GB/sec upload bandwidth
  Download: 200,000/sec x 200 KB = ~40 GB/sec download bandwidth
  (CDN absorbs most download bandwidth)
```

### 9.4 Steady-State Storage (with 30-Day TTL)

```
If media is deleted after 30 days:
  Steady-state storage = daily ingest x 30
  = ~1.3 PB/day x 30 = ~39 PB (conservative)
  = ~3.9 PB/day x 30 = ~117 PB (video-heavy estimate)

With deduplication (forwarded media):
  Assume 20-30% of media items are forwards (no new storage)
  Effective storage: 70-80% of raw calculation
  = ~27-94 PB steady-state

For comparison:
  If media were retained forever (like Telegram):
  After 1 year: ~475 PB - ~1.4 EB
  After 5 years: ~2.4 PB - ~7 EB
  The TTL policy is not just about privacy — it is an existential cost decision.
```

---

## 10. End-to-End Encrypted Media — Security Analysis

### 10.1 The Separation Principle

The core security property of WhatsApp's media architecture is the separation
between the encrypted blob and the decryption key:

```
MEDIA SERVER sees:           MESSAGE SERVER (E2E) carries:
─────────────────────        ─────────────────────────────
  Encrypted blob               mediaId
  mediaId                      AES-256 key (K)
  Upload timestamp              IV
  Blob size                     SHA-256 hash
  Uploader userId               mimeType
                                thumbnail
                                fileSize

                        These are encrypted with Signal
                        Protocol — the message server
                        sees only encrypted bytes.
```

**Even if the media server is fully compromised**, the attacker gets:
- Encrypted blobs (opaque random-looking bytes)
- mediaIds (which blob belongs to which upload)
- Uploader identity and timestamps

The attacker does NOT get:
- Decryption keys (those are in E2E encrypted messages on a different server)
- Plaintext media content
- Information about what the media contains

**Even if the message server is fully compromised**, the attacker gets:
- Encrypted message payloads (opaque bytes — E2E encrypted)
- Metadata: who sent to whom, when, message size

The attacker does NOT get:
- Decryption keys (messages are E2E encrypted; server does not have session keys)
- mediaIds in plaintext (embedded inside E2E encrypted messages)

**Both servers must be compromised AND the E2E encryption must be broken** to
access plaintext media. This is defense in depth.

### 10.2 Threat Model

```
Threat                          Protection
────────────────────────────    ────────────────────────────────
Media server compromise         Blob is encrypted; key is not on
                                media server

Message server compromise       Messages are E2E encrypted; mediaId
                                and key are inside encrypted payload

Network interception (MitM)     HTTPS for upload/download; E2E for
                                message containing key

Compromised CDN edge            CDN caches encrypted blobs only;
                                cannot decrypt

Server operator (insider)       E2E encryption prevents even the
                                operator from reading content

Legal/government request        Server can only hand over encrypted
                                blobs — no plaintext content
```

### 10.3 What IS Exposed (Metadata)

E2E encryption protects content, not metadata. The server knows:

- **Who** uploaded media (uploader userId)
- **When** (upload timestamp)
- **How large** (blob size)
- **Who downloaded it** (download requests include authentication)
- **How often** (download frequency — indicates forwarding)

This metadata can reveal communication patterns even without content access.

---

## 11. Contrast with Telegram

Telegram takes a fundamentally different approach to media:

```
                     WhatsApp                    Telegram
                     ────────                    ────────
Encryption:          E2E (always)                Server-side (default)
                                                 E2E only in Secret Chats

Media storage:       Encrypted blobs             Plaintext on Telegram servers
                     Server cannot read           Server can read and process

Retention:           Temporary (TTL ~30 days)    Permanent (indefinite)

Access from          Only original device         Any device, any time
multiple devices:    (key is on device)           (cloud storage)

Thumbnails:          Client-generated             Server-generated
                     (server cannot see media)    (server has plaintext)

Compression:         Client-side only             Server can re-encode/
                                                  transcode on the fly

Search:              Cannot search media           Server can index media
                     content on server             content

File size limit:     ~16 MB (100 MB for docs)     2 GB
                     [UNVERIFIED]

CDN/caching:         CDN caches encrypted blobs   CDN caches plaintext media
                     (safe — can't decrypt)        (risk if CDN is compromised)
```

**Telegram's advantage**: Seamless multi-device experience. A user can access any
media ever sent or received from their phone, tablet, desktop, or web client. No
need to transfer media between devices. Media links never expire.

**WhatsApp's advantage**: True E2E privacy. Even if Telegram's servers are
compromised (or compelled by a government), all media is accessible in plaintext.
With WhatsApp, a server compromise yields only encrypted blobs.

**Product trade-off**: Telegram prioritizes convenience and features (cloud
storage, large file support, searchable media). WhatsApp prioritizes privacy
(E2E encryption means the server is blind to content). Neither is objectively
better — they serve different user values.

---

## 12. Contrast with Discord

Discord takes yet another approach, optimized for communities rather than
private messaging:

```
                     WhatsApp                    Discord
                     ────────                    ────────
Encryption:          E2E (always)                None (plaintext on server)

Media storage:       Object store,               CDN-backed, permanent
                     TTL-based deletion           public/semi-public URLs

Media URLs:          Temporary, authenticated     Permanent, often public
                                                  (cdn.discordapp.com/...)

Previews/embeds:     Client-generated thumbnail   Server generates rich embeds:
                     (server cannot process)       - Image dimensions + preview
                                                   - Video player embed
                                                   - Link unfurling (OG tags)
                                                   - Audio waveform
                                                   These are IMPOSSIBLE with
                                                   E2E encryption.

Content moderation:  Impossible server-side        Server scans media for:
                     (E2E encrypted)               - NSFW content
                                                   - Malware in files
                                                   - Copyright violations
                                                   - CSAM detection

File size limit:     ~16 MB (basic)               8 MB (free), 50 MB (Nitro)
                     [UNVERIFIED]                  500 MB (Nitro, for video)

Compression:         Aggressive client-side        Minimal — server can
                                                   transcode as needed
```

**Discord's server-side processing advantage**: Because Discord has access to
plaintext media, it can provide a rich experience that is architecturally
impossible in WhatsApp:

1. **Link previews / embeds**: When a user pastes a YouTube link, Discord
   fetches the OG metadata and generates an inline embed with thumbnail, title,
   and description. WhatsApp cannot do this server-side (the URL is inside an
   E2E encrypted message the server cannot read). WhatsApp's link previews are
   generated client-side on the sender's device and included in the message.

2. **Image/video previews**: Discord generates multiple resolution variants
   server-side (thumbnail, preview, full). WhatsApp's server stores one
   encrypted blob — all processing happens on the client.

3. **Content moderation**: Discord scans uploaded media for policy violations
   (NSFW, malware, CSAM). WhatsApp cannot scan E2E encrypted media on the
   server. WhatsApp relies on user reports and client-side heuristics (e.g.,
   detecting forwarded-many-times messages as potential misinformation).

**Discord's design reflects its use case**: public and semi-public communities
where content moderation is essential and privacy from the platform is not
expected. WhatsApp's design reflects private communication where privacy from
everyone (including the platform) is the core promise.

---

## Summary: The Media-Message Separation Pattern

The defining architectural pattern of WhatsApp's media handling is the clean
separation between the media path and the message path:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SENDER CLIENT                                │
│                                                                     │
│   Raw Media                                                         │
│       │                                                             │
│       ├──→ Compress ──→ Encrypt(K) ──→ Upload ─────────────┐        │
│       │                                                    │        │
│       └──→ Generate Thumbnail                              │        │
│               │                                            │        │
│               └──→ Build Message:                          │        │
│                    { mediaId, K, IV,    ←── mediaId ───────┘        │
│                      sha256, thumbnail,                             │
│                      mimeType }                                     │
│                         │                                           │
│                         └──→ E2E Encrypt (Signal Protocol)          │
│                                │                                    │
└────────────────────────────────│────────────────────────────────────┘
                                 │
                    ┌────────────│────────────────┐
                    │   MESSAGE  │  PATH          │
                    │            │                 │
                    │    Chat Server (E2E relay)   │
                    │    Sees: encrypted bytes     │
                    │    Knows: who → whom, when   │
                    │    Cannot: read content      │
                    │            │                 │
                    └────────────│────────────────┘
                                 │
┌────────────────────────────────│────────────────────────────────────┐
│                     RECIPIENT  │ CLIENT                              │
│                                │                                    │
│     E2E Decrypt ←──────────────┘                                    │
│         │                                                           │
│         ├──→ Show thumbnail immediately                             │
│         │                                                           │
│         └──→ Download encrypted blob (mediaId)                      │
│                  │                                                   │
│                  └──→ Verify SHA-256                                 │
│                          │                                           │
│                          └──→ Decrypt(K, IV)                        │
│                                  │                                   │
│                                  └──→ Display full media            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

         ┌──────────────────────────────┐
         │       MEDIA PATH             │
         │                              │
         │  Media Server / CDN          │
         │  Stores: encrypted blobs     │
         │  Knows: mediaId, blob size   │
         │  Cannot: decrypt content     │
         │  TTL: ~30 days               │
         │                              │
         └──────────────────────────────┘
```

This pattern — uploading content to a blob store, referencing it from a
lightweight message, decrypting on the client — is the standard approach for
E2E encrypted media at scale. It separates latency-sensitive message delivery
from throughput-sensitive media transfer, enables CDN caching of encrypted
content, supports deduplication for forwarded media, and keeps media keys
entirely within the E2E encrypted message channel where the server cannot
access them.

The file was created at `/Users/ashwanikumar/IdeaProjects/Leetcode/src/hld/chatapp/design/08-media-handling.md`. Here is a summary of what it covers across its 12 sections:

1. **Section 1 - Why Media is Different**: Explains the fundamental architectural split between text messages (~1 KB, WebSocket, latency-critical) and media (100 KB - 2 GB, HTTPS, throughput-critical). Media content and metadata travel on completely separate paths.

2. **Section 2 - E2E Encrypted Upload Flow**: Full 15-step ASCII diagram showing sender encrypts with random AES-256 key, uploads encrypted blob to media server, sends message with {mediaId, key, IV, SHA-256, thumbnail} through normal E2E message path. Explains why each media item gets its own key.

3. **Section 3 - Chunked Resumable Uploads**: 256 KB chunks with server-tracked upload progress. ASCII diagram of the init/chunk/resume protocol. Analysis of chunk size trade-offs across 3G/4G/Wi-Fi.

4. **Section 4 - Client-Side Compression**: Images resized to max 1600px JPEG (~150-250 KB), videos re-encoded to H.264 Baseline at lower bitrate (~1-3 MB for 10 sec), audio encoded with Opus at ~16 kbps (~60 KB for 30 sec). Explains why compression must happen before encryption (encrypted data is incompressible).

5. **Section 5 - Thumbnail Generation**: Client generates ~32x24 pixel thumbnails (~1-3 KB) included inline in the E2E message. Explains why server-side thumbnail generation is impossible with E2E encryption.

6. **Section 6 - Media Storage Architecture**: Object storage (S3) with CDN delivery. ASCII diagram of the storage tier. TTL-based retention (~30 days) reduces steady-state storage from unbounded growth to ~39-94 PB. Storage class tiering (hot/warm/cold).

7. **Section 7 - Download and Decryption**: Recipient-side flow, auto-download settings per network type (Wi-Fi/cellular/roaming), HTTP range-based resume for failed downloads.

8. **Section 8 - Forwarding and Deduplication**: Forwarded media reuses the same mediaId (no re-upload). One physical copy, many message references. Analysis of deduplication savings (viral image forwarded 1M times: 200 GB reduced to 200 KB).

9. **Section 9 - Scale Numbers**: ~6.5B media items/day, ~75K uploads/sec average, ~1.3-3.9 PB/day new storage, ~15 GB/sec upload bandwidth, ~40 GB/sec download bandwidth.

10. **Section 10 - Security Analysis**: The separation principle (encrypted blob on media server, decryption key in E2E message on chat server). Threat model table. Metadata exposure analysis.

11. **Section 11 - Contrast with Telegram**: Cloud storage, permanent retention, server-side processing, multi-device access. More convenient but not E2E encrypted. Comparison table.

12. **Section 12 - Contrast with Discord**: CDN-backed permanent URLs, server-generated rich embeds/previews, content moderation. All architecturally impossible with E2E encryption. Comparison table.