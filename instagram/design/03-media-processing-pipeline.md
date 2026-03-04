# Media Processing Pipeline — Photo & Video Processing

> The upload and processing pipeline is the write path's most critical component.
> Instagram handles ~95-100 million+ media uploads per day — ~1,200+ uploads/second sustained.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Photo Processing Pipeline](#2-photo-processing-pipeline)
3. [Video Processing Pipeline](#3-video-processing-pipeline)
4. [Thumbnail Generation](#4-thumbnail-generation)
5. [Carousel Handling](#5-carousel-handling)
6. [Content-Aware Processing](#6-content-aware-processing)
7. [Processing Infrastructure](#7-processing-infrastructure)
8. [Scale & Performance](#8-scale--performance)
9. [Contrasts](#9-contrasts)

---

## 1. Overview

Every piece of media on Instagram — feed photos, Stories, Reels, profile avatars, DM images — passes through the media processing pipeline. The pipeline transforms raw user uploads into optimized, multi-resolution assets ready for delivery via CDN.

**The fundamental constraint:** Users expect near-instant publishing. A photo should be visible in followers' feeds within seconds of tapping "Share." This means the processing pipeline must be fast (1-5 seconds for photos, 5-30 seconds for videos) while still producing high-quality, bandwidth-efficient output.

```
                              Media Processing Pipeline
┌─────────────┐    ┌────────────────┐    ┌──────────────────────┐    ┌─────────────┐
│ Client App  │───>│ Upload Service │───>│ Processing Workers   │───>│ Blob Storage│
│ (iOS/Androi │    │ (Resumable)    │    │ (Async Task Queue)   │    │ (Haystack)  │
└─────────────┘    └────────────────┘    │                      │    └──────┬──────┘
                                         │ • Decode             │           │
                                         │ • Strip EXIF         │    ┌──────▼──────┐
                                         │ • Resize (4 sizes)   │    │    CDN      │
                                         │ • Compress           │    │ (Edge PoPs) │
                                         │ • Generate Blurhash  │    └─────────────┘
                                         │ • Content Moderation │
                                         │ • (Video: Transcode) │
                                         └──────────────────────┘
```

---

## 2. Photo Processing Pipeline

### Step-by-Step Flow

```
Raw Photo Upload (JPEG/HEIF/PNG, up to ~30MB)
        │
        ▼
┌─────────────────────────────────────────────┐
│ 1. DECODE                                   │
│    Parse image format. Handle JPEG, HEIF    │
│    (iPhone default since iOS 11), PNG,      │
│    WebP. Validate file integrity.           │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 2. STRIP EXIF METADATA                      │
│    Remove GPS coordinates, camera model,    │
│    serial numbers — privacy-sensitive data.  │
│    Retain: orientation tag (for step 3),     │
│    color space info.                         │
│    Why: Users unknowingly embed their home   │
│    address in photo metadata.                │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 3. AUTO-ORIENT                               │
│    Read EXIF orientation tag (1-8 values).   │
│    Apply rotation/flip to normalize the      │
│    image to upright orientation.              │
│    Why: Different cameras encode orientation  │
│    differently. Without this, photos appear   │
│    rotated on some devices.                   │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 4. RESIZE TO MULTIPLE RESOLUTIONS            │
│                                              │
│    ┌────────────┬──────────────────────────┐ │
│    │ Variant    │ Dimensions / Use Case    │ │
│    ├────────────┼──────────────────────────┤ │
│    │ Thumbnail  │ 150×150 (square crop)    │ │
│    │            │ Profile grid, search     │ │
│    ├────────────┼──────────────────────────┤ │
│    │ Small      │ 320px wide              │ │
│    │            │ Feed on small screens    │ │
│    ├────────────┼──────────────────────────┤ │
│    │ Medium     │ 640px wide              │ │
│    │            │ Feed on standard phones  │ │
│    ├────────────┼──────────────────────────┤ │
│    │ Large      │ 1080px wide             │ │
│    │            │ Feed on high-res phones, │ │
│    │            │ full-screen view         │ │
│    └────────────┴──────────────────────────┘ │
│                                              │
│    Aspect ratio is preserved. Instagram      │
│    supports: 1:1 (square), 4:5 (portrait),  │
│    1.91:1 (landscape). Thumbnails are always │
│    center-cropped to square.                 │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 5. COMPRESS                                  │
│    JPEG quality optimization:                │
│    • Target ~70-80% JPEG quality             │
│    • Balance: file size vs visual quality     │
│    • Instagram-style photos (high color,      │
│      filters) compress well at 75% quality   │
│    • Perceptual quality checks: ensure no     │
│      visible banding or artifacts             │
│                                              │
│    Modern format support:                     │
│    • WebP: ~25-30% smaller than JPEG at      │
│      same quality. Served to Android/Chrome. │
│    • AVIF: ~50% smaller than JPEG.           │
│      Served to supporting clients.            │
│    • Fallback: JPEG for all other clients.    │
│                                              │
│    Format selection at serve time:             │
│    Client sends Accept header →               │
│    CDN returns best supported format.          │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 6. GENERATE BLURHASH PLACEHOLDER             │
│    Encode a low-res color preview as a       │
│    compact string (~20-30 bytes).             │
│    Example: "LGF5]+Yk^6#M@-5c,1J5@[or[Q6." │
│                                              │
│    Why: While the real image loads over the   │
│    network, the client renders the blurhash   │
│    as a colored blur — no jarring blank box.  │
│    The blurhash string is stored in the post  │
│    metadata (not a separate file).            │
│                                              │
│    Blurhash is a ~30 byte string vs a 50KB+   │
│    thumbnail — trivial to include inline in   │
│    API responses.                              │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 7. APPLY FILTERS (if selected)               │
│    Server-side filter application ensures     │
│    consistency across devices. Filters are    │
│    defined as color transformation matrices.  │
│                                              │
│    Why server-side? If filters were applied   │
│    only on the client, the same filter would  │
│    look different on different devices due to │
│    color profile and display differences.     │
│    Server-side application is canonical.      │
│    [INFERRED — not officially documented]     │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 8. CONTENT MODERATION (ML scan)              │
│    Run in parallel with resize/compress:      │
│    • Nudity detection (computer vision)       │
│    • Violence/graphic content detection       │
│    • Hate symbol detection                    │
│    • Text extraction (OCR) → hate speech NLP  │
│    • PDQ perceptual hash → check against      │
│      known-violating image database            │
│                                              │
│    Verdicts: APPROVED, HELD_FOR_REVIEW,       │
│    REJECTED, AGE_GATED, REDUCED_DISTRIBUTION  │
│                                              │
│    See: Meta's SimSearchNet, PDQ hashing,     │
│    Few-Shot Learner, RoBERTa-based text       │
│    classifiers (all verified from Meta AI     │
│    publications).                              │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 9. UPLOAD TO BLOB STORAGE + CDN              │
│    All variants uploaded to Haystack (hot)    │
│    or f4 (warm, erasure-coded).               │
│    CDN URLs generated for each variant.       │
│    Media metadata (URLs, dimensions, format,  │
│    blurhash) stored in database.               │
│    Return mediaId to client.                   │
└─────────────────────────────────────────────┘
```

### Latency Budget (Photo Processing)

| Step | Typical Duration | Notes |
|---|---|---|
| Upload (15MB photo on LTE) | ~2-5 seconds | Network-bound, resumable |
| Decode + Strip EXIF | ~50ms | CPU-bound |
| Auto-orient | ~20ms | Simple matrix operation |
| Resize (4 variants) | ~200ms | Parallelizable across variants |
| Compress (JPEG + WebP) | ~300ms | CPU-intensive, quality tuning |
| Generate blurhash | ~50ms | Lightweight |
| Content moderation (ML) | ~500ms-2s | GPU inference, runs in parallel |
| Upload to blob storage | ~200ms | Internal network |
| **Total processing** | **~1-3 seconds** | Excluding upload time |

---

## 3. Video Processing Pipeline

Video processing is significantly more complex and time-consuming than photo processing.

### Step-by-Step Flow

```
Raw Video Upload (MP4/MOV, up to 60s feed / 90s Reels / 15s Story)
        │
        ▼
┌─────────────────────────────────────────────┐
│ 1. EXTRACT METADATA                          │
│    Duration, resolution, codec (H.264/H.265/│
│    VP9), frame rate, audio codec, bitrate.   │
│    Validate: duration within limits,          │
│    resolution acceptable, format supported.   │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 2. TRANSCODE TO MULTIPLE RESOLUTIONS         │
│                                              │
│    ┌────────────┬──────────┬───────────────┐ │
│    │ Resolution │ Bitrate  │ Use Case      │ │
│    ├────────────┼──────────┼───────────────┤ │
│    │ 360p       │ ~500kbps │ Poor network  │ │
│    │ 480p       │ ~1Mbps   │ Cellular      │ │
│    │ 720p       │ ~2.5Mbps │ Standard WiFi │ │
│    │ 1080p      │ ~5Mbps   │ High-res      │ │
│    └────────────┴──────────┴───────────────┘ │
│                                              │
│    Codec: H.264 (universal compatibility).   │
│    Instagram uses FIXED encoding ladders,    │
│    NOT per-title optimization (unlike        │
│    Netflix). At 100M+ uploads/day, per-title │
│    analysis is computationally infeasible.   │
│                                              │
│    Audio: Transcode to AAC (128kbps stereo). │
│    Normalize audio levels for consistent     │
│    volume across Reels/Stories.               │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 3. GENERATE HLS SEGMENTS                     │
│    Split each resolution into 2-4 second     │
│    segments for adaptive bitrate streaming.   │
│    Generate M3U8 playlist file listing all    │
│    quality levels and segment URLs.           │
│                                              │
│    Why HLS? Instagram is mobile-first.        │
│    HLS (Apple's protocol) works natively on   │
│    iOS. Android supports HLS well via          │
│    ExoPlayer. DASH is less common on mobile.  │
│                                              │
│    Each segment starts with a keyframe (IDR   │
│    frame) — independently decodable. This     │
│    enables mid-stream quality switching.       │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 4. EXTRACT KEYFRAMES + THUMBNAILS            │
│    Extract frames at regular intervals        │
│    (every 1 second). Pick the most visually   │
│    interesting frame as the default cover:     │
│    • Saliency detection (ML model identifies  │
│      visually interesting regions)             │
│    • Face detection (prefer frames with faces)│
│    • Blur detection (avoid blurry frames)     │
│    • User can override by selecting a custom   │
│      cover frame in the app.                   │
│                                              │
│    Generate thumbnail at 150×150 (square      │
│    crop) for the grid view.                    │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 5. CONTENT MODERATION (parallel with above)  │
│    Video moderation is more expensive than    │
│    photo moderation:                          │
│    • Sample frames at ~1 fps → run image      │
│      classifiers on each sampled frame        │
│    • Audio analysis: detect copyrighted       │
│      music (fingerprinting), hate speech      │
│    • Video-level classifiers (SlowFast        │
│      networks) for action/context detection   │
│    • Caption/text overlay OCR → NLP           │
│                                              │
│    For Reels, copyright detection is critical │
│    — uses audio fingerprinting against a      │
│    music catalog (similar to YouTube's         │
│    Content ID but for audio only).             │
│    [INFERRED — specific approach not          │
│    officially documented by Instagram]        │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 6. UPLOAD ALL VARIANTS TO STORAGE + CDN      │
│    • All resolution variants                  │
│    • HLS segments + playlist                  │
│    • Thumbnails                                │
│    • Store metadata (durations, URLs, etc.)   │
│    • Return mediaId                            │
└─────────────────────────────────────────────┘
```

### Latency Budget (Video Processing — 30-second Reel)

| Step | Typical Duration | Notes |
|---|---|---|
| Upload (50MB video on WiFi) | ~5-15 seconds | Network-bound, resumable |
| Metadata extraction | ~100ms | Fast, header parsing |
| Transcode (4 resolutions) | ~10-30 seconds | CPU/GPU-bound, parallelizable |
| HLS segmentation | ~2-5 seconds | Splits + packages |
| Keyframe extraction | ~1-2 seconds | Frame sampling + saliency |
| Content moderation | ~3-10 seconds | GPU inference, runs in parallel |
| Upload to storage | ~1-3 seconds | Internal network |
| **Total processing** | **~10-30 seconds** | Excluding upload time |

---

## 4. Thumbnail Generation

Every post needs a thumbnail for grid views (profile page, Explore page, hashtag pages, search results).

**Photos:**
- Center-crop to 1:1 (square) at 150×150 pixels
- If the photo is already square, just resize
- If portrait (4:5), crop top and bottom equally
- If landscape (1.91:1), crop left and right equally
- Use smart cropping with face detection — if faces are detected, center the crop on the face region rather than the geometric center

**Videos:**
- Extract first 3-5 frames
- Run saliency detection to pick the most visually interesting frame
- Apply the same square-crop logic as photos
- User can override by selecting a custom cover frame
- For Reels, the cover frame also appears in the Reels grid on the profile page

**Carousel Posts:**
- Use the first item's thumbnail as the carousel's grid thumbnail
- A small carousel indicator icon overlays the thumbnail

---

## 5. Carousel Handling

Carousel posts contain up to 10 photos/videos.

**Processing flow:**
1. Each media item is uploaded independently (separate upload sessions)
2. Each item goes through the full processing pipeline independently
3. The post is created only after ALL items have completed processing
4. If any item fails content moderation, the entire post is held

**Implementation:**
```
POST /posts (carousel)
    mediaIds: [media-1, media-2, ..., media-10]
        │
        ▼
    For each mediaId:
        Check processing status == COMPLETED
        Check moderation verdict == APPROVED
        │
        ▼
    All passed? → Create post, trigger fan-out
    Any failed? → Return error with failed mediaId
```

**Why this matters architecturally:**
- Client-side: User selects all photos/videos, then taps Share. Under the hood, each is uploaded and processed independently. The "Share" button is disabled until all items report COMPLETED.
- Server-side: Must track processing state for a "batch" of media items tied to a single post creation intent. If the user abandons before sharing, the orphaned media is garbage-collected after a timeout.

---

## 6. Content-Aware Processing

Instagram applies ML-based content understanding during processing:

**Auto-enhance (optional):**
- Brightness/contrast auto-adjustment using scene understanding
- Not applied by default (only when user enables "Enhance" filter)

**Face detection:**
- Used for: auto-focus regions, smart cropping (center crop on faces), people tagging suggestions
- Meta's face detection models (DeepFace lineage) are state-of-the-art
- [Note: Meta disabled automatic face recognition tagging in November 2021 following privacy concerns, but face detection for non-identifying purposes like smart cropping may still be used — PARTIALLY VERIFIED]

**Alt text generation:**
- Instagram auto-generates alt text for photos using object detection
- Example: "Photo may contain: 2 people, smiling, outdoor, sky, beach"
- This powers accessibility features (screen readers)
- Users can override with custom alt text

**Object and scene detection:**
- Used for: Explore recommendations (understanding what's in the photo), content categorization, search indexing
- Powers features like "Photos of you" suggestions and topic-based Explore recommendations

---

## 7. Processing Infrastructure

### Async Task Queue Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────────┐
│ Upload       │────>│ Task Queue       │────>│ Processing Workers    │
│ Service      │     │ (SQS-like /      │     │ (Stateless, auto-    │
│              │     │  Celery-like)     │     │  scaled fleet)       │
└──────────────┘     └──────────────────┘     └──────────────────────┘
       │                                              │
       ▼                                              ▼
  Post created with                            On completion:
  status: PROCESSING                           - Update status → PUBLISHED
                                               - Store media metadata
                                               - Trigger feed fan-out
```

**Why async?**
- Decouples upload latency from processing latency. User sees immediate feedback ("uploading...") while processing happens in the background.
- Processing workers are stateless — can be horizontally scaled independently of upload servers.
- If a processing worker crashes, the task queue retries on another worker.
- Different media types have different processing costs (photo: 1-3s, video: 10-30s). Async processing handles this heterogeneity naturally.

**Instagram's early stack used Celery (Python task queue) with Redis as the broker.** [VERIFIED — from Instagram's 2012 engineering blog "What Powers Instagram"]. At Meta scale, they likely use a more sophisticated internal task queue system, but the pattern is the same.

### Worker Fleet Sizing

Back-of-envelope calculation:
- ~100M uploads/day = ~1,200 uploads/second
- Average processing time: ~3 seconds (weighted average of photos and videos)
- Required concurrency: 1,200 × 3 = **~3,600 concurrent processing tasks**
- With overhead and burst capacity (2-3x): **~7,000-10,000 processing workers**
- Peak hours (evening in populous time zones) can be 3-5x the average
- Workers are auto-scaled based on queue depth

---

## 8. Scale & Performance

| Metric | Value | Confidence |
|---|---|---|
| Uploads per day | ~95-100 million+ | MEDIUM (2016 official, likely higher now) |
| Uploads per second (sustained) | ~1,200+ | DERIVED from daily figure |
| Photo processing time | ~1-3 seconds | INFERRED |
| Video processing time (30s clip) | ~10-30 seconds | INFERRED |
| Resolutions per photo | 4 (150px, 320px, 640px, 1080px) | INFERRED |
| Resolutions per video | 4 (360p, 480p, 720p, 1080p) | INFERRED |
| Image formats | JPEG, WebP, AVIF | VERIFIED (WebP widely used) |
| Video codec | H.264 (primary), H.265/VP9 (emerging) | INFERRED |
| Encoding ladder | Fixed (NOT per-title) | INFERRED |

---

## 9. Contrasts

### Instagram vs YouTube — Processing Philosophy

| Dimension | Instagram | YouTube |
|---|---|---|
| **Content type** | Short-form (photos, 15-90s videos) | Long-form (minutes to hours) |
| **Volume** | 100M+ uploads/day | 500+ hours uploaded/minute |
| **Encoding strategy** | Fixed encoding ladder | Fixed ladder (most), per-title for premium |
| **Processing priority** | Speed (near-instant publishing) | Quality (users accept minutes of processing) |
| **Per-title optimization** | Infeasible at volume | Infeasible for UGC, used for premium |
| **Video duration** | 15-90 seconds | Minutes to hours |
| **Primary format** | Photos (still dominant) | Video (exclusively) |

**Key insight:** Instagram and YouTube have similar volumes of user-generated uploads, but Instagram's content is much smaller per-item (KB-MB for photos, tens of MB for short videos vs GB for YouTube long-form). Instagram optimizes for processing speed; YouTube optimizes for encoding quality.

### Instagram vs Netflix — Processing Philosophy

| Dimension | Instagram | Netflix |
|---|---|---|
| **Content source** | User-generated (100M+/day) | Professionally produced (hundreds/week) |
| **Encoding strategy** | Fixed ladder | Per-title optimized (VMAF, convex hull) |
| **Compute per item** | Seconds | Hours (20x more compute per title) |
| **Justification** | Must be fast — users expect instant | Encode once, stream billions of times |
| **Quality metric** | "Good enough" (JPEG quality ~75%) | VMAF perceptual quality optimization |
| **Codecs** | H.264 primary | H.264, VP9, AV1 |

**Key insight:** Netflix can afford to spend 20x more compute per title because each title is watched billions of times — the compute cost is amortized. Instagram can't — each photo is viewed hundreds to thousands of times, and there are 100M+ uploads per day. Speed trumps encoding optimization.

### Instagram vs Snapchat — Storage Implications

| Dimension | Instagram | Snapchat |
|---|---|---|
| **Permanent content** | Feed posts, Reels (permanent) | Minimal (Spotlight) |
| **Ephemeral content** | Stories (24-hour TTL) | Everything (default ephemeral) |
| **Storage strategy** | Dual: permanent (Haystack) + ephemeral (TTL) | Ephemeral-first |
| **Complexity** | Higher (two storage tiers) | Lower (one storage tier) |

**Key insight:** Instagram was designed for permanent content and bolted on ephemeral (Stories) later. Snapchat was designed ephemeral-first. This architectural difference means Instagram must manage two distinct storage lifecycles, migration between them (Stories → Highlights), and different caching strategies for each.
