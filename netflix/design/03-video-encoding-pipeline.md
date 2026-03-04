# Netflix Video Encoding Pipeline — Deep Dive

## Overview

Netflix's video encoding pipeline transforms source masters into thousands of optimized streams
that adapt to every device and network condition on the planet. The pipeline is a
massively parallel, DAG-orchestrated system that treats encoding as an offline batch problem —
encode once with maximum quality optimization, then stream billions of times.

---

## 1. Source Ingestion

Content providers (studios, post-production houses) deliver **source masters** to Netflix:

| Property | Typical Value |
|---|---|
| Resolution | 4K (3840x2160) or higher |
| Format | Apple ProRes 4444 or JPEG 2000 IMF packages |
| Color depth | 10-bit or 12-bit HDR |
| Size per title | Hundreds of GB (feature film can exceed 500 GB) |
| Audio | Uncompressed PCM, multiple language tracks |

### Upload Flow

```
Content Provider
       |
       v
+-------------------+
| Chunked Resumable |  -- Multipart upload to S3
|     Upload        |  -- Checksummed per chunk
+-------------------+  -- Retryable on failure
       |
       v
+-------------------+
|   S3 Landing Zone |  -- Regional bucket near provider
+-------------------+
       |
       v
+-------------------+
|  Validation &     |  -- Format checks, checksum verify
|  Registration     |  -- Metadata extraction (resolution, codec, duration)
+-------------------+
       |
       v
   Pipeline Trigger
```

**Why chunked resumable uploads?** Source files are hundreds of GB. A single TCP connection
over hours is fragile. Chunking (typically 64-128 MB parts) allows:
- Resume after network failures without re-uploading the entire file
- Parallel upload of multiple chunks
- Per-chunk integrity verification (MD5/SHA-256)
- S3 multipart upload API maps directly to this model

---

## 2. Transcoding DAG

The encoding pipeline is orchestrated as a **Directed Acyclic Graph (DAG)** of tasks. Each node
is an independent, retryable unit of work. The DAG ensures correct ordering while maximizing
parallelism.

### Pipeline DAG Structure

```
                         +------------------+
                         |  Source Master    |
                         |  (S3)            |
                         +--------+---------+
                                  |
                                  v
                         +------------------+
                         |  Video Decode    |
                         |  (demux + decode)|
                         +--------+---------+
                                  |
                                  v
                         +------------------+
                         | Scene / Shot     |
                         | Detection        |
                         +--------+---------+
                                  |
                                  v
                         +------------------+
                         | Per-Scene Quality|
                         | Analysis (VMAF)  |
                         +--------+---------+
                                  |
                    +-------------+-------------+
                    |             |             |
                    v             v             v
            +-----------+ +-----------+ +-----------+
            | Encode    | | Encode    | | Encode    |   N resolution x codec
            | 1080p/AV1 | | 720p/VP9  | | 480p/H264 |   combinations in
            +-----------+ +-----------+ +-----------+   parallel
                    |             |             |
                    v             v             v
            +-----------+ +-----------+ +-----------+
            | Segment   | | Segment   | | Segment   |   CMAF / fMP4
            | Packaging | | Packaging | | Packaging |   segments
            +-----------+ +-----------+ +-----------+
                    |             |             |
                    +-------------+-------------+
                                  |
                                  v
                         +------------------+
                         | Upload Segments  |
                         | to S3 (CDN Origin)|
                         +--------+---------+
                                  |
                                  v
                         +------------------+
                         | Register in      |
                         | Content Catalog  |
                         +------------------+
```

### DAG Properties

| Property | Detail |
|---|---|
| Orchestrator | Cosmos (Netflix's media pipeline platform) |
| Task isolation | Each node runs in its own container |
| Retryability | Failed nodes retry independently without re-running upstream |
| Fan-out | Quality analysis fans out into N parallel encode jobs |
| Fan-in | Segment upload waits for all encodes to complete |
| Metadata flow | Each node writes metadata (bitrate, VMAF score, timing) to a shared store |

---

## 3. Codecs

Netflix employs a **multi-codec strategy**, selecting the best codec each device supports.

### Codec Comparison

| Codec | Standard | Efficiency vs H.264 | Royalty | Device Support | Netflix Role |
|---|---|---|---|---|---|
| **H.264 (AVC)** | ITU-T / ISO | Baseline (1x) | Licensed (MPEG-LA) | Universal | Fallback for all devices |
| **VP9** | Google / WebM | ~35% better | Royalty-free | Chrome, Android, Smart TVs | Mid-tier efficiency |
| **AV1** | Alliance for Open Media | ~48% better | Royalty-free | Growing rapidly | Primary codec for supported devices |

### AV1 at Netflix (Current State)

AV1 has become Netflix's flagship codec:

- **30% of all Netflix streaming** is now AV1 (as of December 2025)
- AV1 sessions consume **1/3 less bandwidth** than equivalent H.264 sessions
- **45% fewer buffering interruptions** on AV1 streams
- **AV1 HDR** launched March 2025, covering ~85% of the HDR catalog
- **Film Grain Synthesis** productized July 2025 — instead of encoding noisy film grain
  at high bitrate, the encoder strips grain and embeds synthesis parameters; the decoder
  reconstructs grain at playback. Massive bitrate savings for grainy content.
- Encoding cost: **10-100x slower** than H.264 encoding. This is acceptable because Netflix
  encodes offline (encode once, stream billions of times).

> Source: [AV1 Now Powering 30% of Netflix Streaming](https://netflixtechblog.com/av1-now-powering-30-of-netflix-streaming-02f592242d80)

### AV1 Film Grain Synthesis — How It Works

```
Source with natural film grain
         |
         v
+---------------------+
| Grain Analysis      |  -- Detect grain pattern, intensity, correlation
+---------------------+
         |
         v
+---------------------+
| Grain Removal       |  -- Denoise the source
| + Parameter Extract |  -- Store grain model as side metadata
+---------------------+
         |
         v
+---------------------+
| Encode Clean Video  |  -- Much lower bitrate (smooth content compresses well)
| + Grain Metadata    |  -- Tiny overhead for grain parameters
+---------------------+
         |
         v
  On Client Decoder:
+---------------------+
| Decode Clean Video  |
| + Synthesize Grain  |  -- AV1 decoder applies grain from metadata
+---------------------+
         |
         v
  Perceptually identical output at a fraction of the bitrate
```

### Contrast: Netflix vs YouTube Codec Strategy

| Dimension | Netflix | YouTube |
|---|---|---|
| Codec selection | Per-device, best available | Per-device, best available |
| Encoding profiles | Per-title optimized ladder | Fixed encoding ladder |
| AV1 adoption | 30% of streams (Dec 2025) | Broad on popular content |
| Encoding time budget | Hours to days (offline) | Minutes (near-real-time) |
| Key driver | Quality per bit | Availability speed |

---

## 4. Encoding Ladder

The **encoding ladder** defines which resolution-bitrate combinations are available for
adaptive bitrate streaming (ABR). The client switches between rungs based on network conditions.

### Historical Fixed Ladder

Netflix's original fixed ladder (circa 2015):

| Rung | Resolution | Bitrate (kbps) |
|---:|---|---:|
| 1 | 320 x 240 | 235 |
| 2 | 384 x 288 | 375 |
| 3 | 512 x 384 | 560 |
| 4 | 512 x 384 | 750 |
| 5 | 640 x 480 | 1,050 |
| 6 | 720 x 480 | 1,750 |
| 7 | 1280 x 720 | 2,350 |
| 8 | 1280 x 720 | 3,000 |
| 9 | 1920 x 1080 | 4,300 |
| 10 | 1920 x 1080 | 5,800 |

**Problem**: A visually simple animated show (e.g., *My Little Pony*) looks perfect at 1.5 Mbps
1080p, while a complex action movie (e.g., *The Dark Knight*) still shows artifacts at 5.8 Mbps.
A fixed ladder wastes bits on simple content and starves complex content.

### Scale of Encoding

Netflix generates approximately **~120 encoding profiles per title** — combinations of:
- Multiple resolutions (240p through 4K)
- Multiple codecs (H.264, VP9, AV1)
- Multiple bitrates per resolution
- HDR and SDR variants
- Audio codecs and languages (handled in a parallel pipeline)

---

## 5. Per-Title Encoding Optimization

Per-title encoding is Netflix's landmark contribution to video streaming. Instead of one fixed
ladder for all content, **each title gets a custom bitrate ladder** tuned to its visual complexity.

> Source: [Per-Title Encode Optimization](https://netflixtechblog.com/per-title-encode-optimization-7e99442b62a2)

### VMAF — Video Multi-Method Assessment Fusion

Netflix developed **VMAF** as an open-source perceptual video quality metric.

| Property | Detail |
|---|---|
| Scale | 0 to 100 (100 = indistinguishable from source) |
| Approach | Machine learning fusion of multiple elementary metrics |
| Components | Visual Information Fidelity (VIF), Detail Loss Metric (DLM), motion features |
| Training data | Thousands of human subjective quality ratings |
| Advantage over PSNR/SSIM | Correlates much better with human perception |
| Open source | github.com/Netflix/vmaf |

VMAF is the **objective function** that the entire encoding optimization maximizes.

### Convex Hull Optimization

The core algorithm behind per-title encoding:

```
Step 1: Encode at MANY resolution x bitrate combinations
         (e.g., hundreds of encodes per title)

Step 2: For each encode, compute VMAF score

Step 3: Plot VMAF vs Bitrate for each resolution

              VMAF
         100 |                          ___-------  4K
              |                   __---'
          90 |              __--'           ___---- 1080p
              |          _-'          __---'
          80 |       _-'        __--'        ___--- 720p
              |     _'      _--'        __--'
          70 |   .'     _-'        __-'
              |  /    _-'      _--'          ___--- 480p
          60 | /  _-'     __-'         __--'
              |/_-'    _--'        __--'
          50 |'   __-'        __-'
              | --'       __-'
          40 |       __-'
              |   _-'
              +--+--------+--------+--------+----> Bitrate
              0  1000    2000     4000     8000  kbps

Step 4: Compute the CONVEX HULL across all resolutions
         (Pareto-optimal points: maximum VMAF for each bitrate)

Step 5: The convex hull points become the encoding ladder for this title
```

**Key insight**: At low bitrates, a lower resolution at the same bitrate often has **higher
VMAF** than a higher resolution, because the lower resolution avoids compression artifacts.
The convex hull automatically discovers these crossover points per title.

### Results

| Codec | Bitrate Reduction at Same VMAF | Notes |
|---|---|---|
| H.264 | ~28% | Compared to fixed ladder |
| VP9 | ~38% | Greater gains due to codec flexibility |
| HEVC | ~34% | Licensed codec, used on specific devices |
| 4K streams | ~50% | 8 Mbps vs 16 Mbps fixed |

### Compute Trade-off

| Aspect | Fixed Ladder | Per-Title Optimized |
|---|---|---|
| Encodes per title | ~10 | Hundreds (analysis) + ~120 (final) |
| Compute cost | 1x | ~20x |
| Bitrate savings | Baseline | 28-50% depending on codec |
| Justification | — | Encode once, stream billions of times. CDN and bandwidth savings dwarf compute cost. |

The math is straightforward: if a title is streamed 10 million times and per-title saves
1 Mbps average, that is 10 million * 1 Mbps of bandwidth saved — vastly exceeding the
one-time ~20x compute overhead.

---

## 6. Shot-Based Encoding

Shot-based encoding is the **evolution of per-title optimization**. Instead of one ladder
per title, each **shot** (continuous segment between cuts) gets its own optimal bitrate.

### Why Shot-Based?

A single movie alternates between:
- Dark, static dialogue scenes (very compressible)
- Bright, fast action sequences (hard to compress)
- Transitions, credits, establishing shots (variable)

Per-title optimization uses one ladder for the whole title, which is a compromise.
Shot-based encoding removes that compromise.

### Shot-Based Pipeline

```
Source Video
     |
     v
+------------------+
| Shot Detection   |  -- Detect scene/shot boundaries
+------------------+     (cuts, fades, dissolves)
     |
     v
+---------+---------+---------+---------+
| Shot 1  | Shot 2  | Shot 3  | Shot N  |   Independent
| (dialog)| (action)| (dark)  |  ...    |   analysis
+---------+---------+---------+---------+
     |         |         |         |
     v         v         v         v
+---------+---------+---------+---------+
|Convex   |Convex   |Convex   |Convex   |   Per-shot
|Hull Opt |Hull Opt |Hull Opt |Hull Opt |   optimization
+---------+---------+---------+---------+
     |         |         |         |
     v         v         v         v
| 1.2 Mbps| 4.8 Mbps| 0.8 Mbps| varies |   Each shot gets
| 720p    | 1080p   | 720p    |        |   its own bitrate
+---------+---------+---------+---------+
     |         |         |         |
     +----+----+----+----+----+----+
          |
          v
   Concatenate with seamless
   bitrate switching between shots
```

### Shot-Based vs Per-Title

| Dimension | Per-Title | Shot-Based |
|---|---|---|
| Granularity | One ladder per title | One ladder per shot |
| Efficiency | Good | Better (10-20% additional savings over per-title) |
| Compute cost | ~20x baseline | Even higher (analysis per shot) |
| Complexity | Moderate | High (seamless shot concatenation, variable segment sizes) |
| Adaptiveness | Title-level average | Matches bitrate to visual complexity frame-by-frame |

---

## 7. Pipeline Performance

### Parallelism Model

Netflix's encoding workload is **embarrassingly parallel** at multiple levels:

```
Level 1: Title-level parallelism
  -- Hundreds of titles encode simultaneously across the fleet

Level 2: Codec/Resolution parallelism
  -- Each of the ~120 profiles encodes independently

Level 3: Chunk-level parallelism
  -- Each title is split into temporal chunks (typically 2-4 seconds)
  -- Each chunk encodes independently on a separate worker
  -- Chunks are stitched after encoding

Level 4: Frame-level parallelism (within encoder)
  -- Modern encoders use multiple threads for motion estimation, etc.
```

### Performance Numbers

| Metric | Value |
|---|---|
| 1080p full encode | ~30 minutes (parallelized) |
| 4K full encode | Several hours |
| AV1 encode time | 10-100x slower than H.264 per frame |
| Parallelism factor | Hundreds of workers per title |
| Total profiles per title | ~120 |
| Infrastructure | Runs on AWS (Netflix is largest AWS customer) |

### Why Offline Encoding Works for Netflix

Netflix content is **pre-encoded and cached**. Unlike live streaming or user-uploaded content,
there is no urgency to make content available in seconds. A new title can take hours or even
a day to fully encode across all profiles — this is acceptable because:

1. Content is scheduled for release days/weeks in advance
2. Encoding cost is amortized over billions of streams
3. More compute time = better quality optimization = less bandwidth per stream
4. CDN pre-warming can happen after encoding completes

---

## 8. Netflix vs YouTube — Full Comparison

This contrast illuminates fundamental architectural trade-offs driven by different business models.

### Volume

| Metric | Netflix | YouTube |
|---|---|---|
| Ingest rate | Hundreds of titles per week | 500+ hours of video per minute |
| Content type | Professional studio masters | User-generated, highly variable |
| Source quality | 4K ProRes / IMF (controlled) | Anything from phone video to 8K (uncontrolled) |
| Catalog size | ~17,000 titles | 800+ million videos |

### Encoding Strategy

| Dimension | Netflix | YouTube |
|---|---|---|
| Ladder type | **Per-title** (custom per content) | **Fixed** (same ladder for all, with some per-category tuning) |
| Optimization | Convex hull, shot-based | Fixed resolution-bitrate mappings |
| Why | Fewer titles, encode once serve forever | Sheer volume makes per-title infeasible at scale |
| Encode budget | Hours to days | Must be available in minutes |
| AV1 usage | 30% of streams, growing | Applied to popular/high-traffic content |

### Quality Metric

| Dimension | Netflix | YouTube |
|---|---|---|
| Primary metric | **VMAF** (ML-based perceptual model) | SSIM / PSNR (simpler, faster to compute) |
| Calibration | Trained on Netflix content + human ratings | Tuned for speed and broad applicability |
| Cost to compute | Expensive (part of convex hull analysis) | Must be cheap at YouTube's scale |
| Open source | Yes (github.com/Netflix/vmaf) | Internal tooling |

### Time Pressure

| Dimension | Netflix | YouTube |
|---|---|---|
| Availability SLA | Days before release date | Minutes after upload |
| Re-encoding | Can re-encode entire catalog with new codec | Impractical for 800M+ videos |
| Optimization depth | Deep (hundreds of trial encodes) | Shallow (encode fast, ship fast) |

### Architectural Philosophy

```
Netflix:                              YouTube:

  Quality per bit is KING             Availability speed is KING
        |                                     |
        v                                     v
  Spend 20x compute to save           Encode in minutes, accept
  28-50% bandwidth forever            higher bitrate, improve later
        |                                     |
        v                                     v
  Offline batch pipeline              Near-real-time pipeline
  (Cosmos orchestrator)               (must handle 500+ hrs/min)
        |                                     |
        v                                     v
  Per-title, per-shot                 Fixed ladder, opportunistic
  convex hull optimization            re-encoding for popular content
        |                                     |
        v                                     v
  Hundreds of titles/week             500+ hours/minute
  = tractable for deep analysis       = must be industrialized
```

### Summary Table

| Dimension | Netflix | YouTube |
|---|---|---|
| Business model | Subscription (fixed revenue) | Ad-supported + Premium |
| Content control | Full (studio masters) | None (user-uploaded anything) |
| Volume | Low (hundreds/week) | Extreme (500+ hrs/min) |
| Encoding strategy | Per-title optimized | Fixed ladder |
| Quality metric | VMAF | SSIM/PSNR |
| Codec strategy | H.264 / VP9 / AV1 | H.264 / VP9 / AV1 |
| Time to available | Hours-days | Minutes |
| Re-encoding | Entire catalog periodically | Only popular content |
| Compute trade-off | Spend more compute, save bandwidth | Spend less compute, accept higher bitrate |

---

## Key Takeaways

1. **Encode once, stream billions of times** — Netflix's entire encoding philosophy.
   Any amount of one-time compute is justified if it saves bandwidth at scale.

2. **VMAF as the objective function** — A perceptual quality metric that drives all
   optimization decisions, from convex hull analysis to codec selection.

3. **AV1 is the future** — 48% more efficient than H.264, already 30% of Netflix streams.
   Film Grain Synthesis and HDR support accelerating adoption.

4. **Per-title and per-shot optimization** — Custom encoding ladders remove the waste
   inherent in one-size-fits-all approaches. The convex hull algorithm automatically
   discovers optimal resolution-bitrate trade-offs.

5. **The Netflix-YouTube contrast reveals a fundamental trade-off** — When you control
   content and have time, optimize deeply. When you process the world's video in real-time,
   optimize for speed and iterate later.
