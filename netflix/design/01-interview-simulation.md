# System Design Interview Simulation: Design Netflix (Video Streaming Platform)

> **Interviewer:** Principal Engineer (L8), Netflix Streaming Infrastructure Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 19, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm on the streaming infrastructure team at Netflix. For today's system design round, I'd like you to design a **video streaming platform** — think Netflix. Not just a video player — I'm talking about the full end-to-end system: content ingestion, encoding, delivery to hundreds of millions of users worldwide, recommendations that drive discovery, and the resilience engineering that keeps it all running.

I care about how you think about scale, content delivery, and the tradeoffs that make streaming at this level work. I'll push on your choices — that's calibration, not criticism.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Netflix is a massive system, so let me scope this before diving into architecture. Video streaming spans encoding, delivery, personalization, and reliability — each a deep topic on its own.

**Functional Requirements — what operations do we need?**

> "Let me identify the core user-facing operations:
>
> - **Browse & Search** — Users browse a personalized home page of content rows, search by title/actor/genre
> - **Play Video** — The most critical path. User presses play, video starts within ~2 seconds, adapts quality based on network conditions
> - **Resume Playback** — Remember where the user left off across devices
> - **Manage Profiles** — Up to 5 profiles per account, each with independent viewing history and recommendations
> - **My List** — User-curated watchlist
> - **Rate Content** — Thumbs up/down that feeds the recommendation engine
>
> And on the content side:
> - **Ingest Content** — Content partners upload source masters (4K ProRes, hundreds of GB each)
> - **Transcode** — Encode each title into multiple resolution/bitrate/codec combinations
> - **Publish** — Make transcoded assets available in the catalog across regions
>
> A few clarifying questions:
> - **Are we designing for live streaming too?** Netflix recently started live events (Jake Paul vs Mike Tyson hit 65 million concurrent streams)."

**Interviewer:** "Good awareness. Focus on VOD — that's 99%+ of Netflix traffic. Mention live as a scaling challenge but don't deep-dive it."

> "- **Should I cover the ad-supported tier?** Netflix launched an ad tier in 2022."

**Interviewer:** "Mention it architecturally but don't deep-dive ads. Focus on the core streaming path."

> "- **Geographic scope?** Netflix operates in 190+ countries."

**Interviewer:** "Yes, global. That's what makes the CDN discussion interesting."

**Non-Functional Requirements:**

> "Now the critical constraints. Netflix is defined by its non-functional properties:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Latency** | < 2 seconds from press-play to first frame | Users abandon if buffering takes too long. Budget: DNS (~50ms) → API (~100ms) → DRM license (~200ms) → first segment from CDN (~500ms) → decode+render (~200ms) |
> | **Zero buffering** | No rebuffering at steady state | Adaptive bitrate switching handles bandwidth fluctuations seamlessly |
> | **Availability** | 99.99% (four 9's) | ~52 min downtime/year. Netflix serves 190+ countries — outage = global headline |
> | **Scale** | 301M subscribers, 65M peak concurrent streams | Q4 2024: 301.63M paid memberships. Jake Paul vs Tyson: 65M concurrent streams |
> | **CDN efficiency** | 95%+ traffic from edge | Open Connect serves 95% of traffic from OCAs inside ISP networks |
> | **Personalization** | 75-80% of viewing driven by recommendations | Recommendations save Netflix >$1B/year in reduced churn |
> | **Content freshness** | New releases available globally at midnight local time | Content must be pre-positioned on all OCAs before launch |
> | **Multi-device** | Same experience across TV, mobile, tablet, browser | Different DRM per platform: Widevine (Android/Chrome), FairPlay (Apple), PlayReady (Windows/Xbox) |

**Interviewer:**
Good scoping. You mentioned the 2-second playback latency budget — I want to come back to that during the CDN discussion. Let me ask: why did you call out the recommendation engine in non-functional requirements?

**Candidate:**

> "Because personalization isn't just a feature — it's the core value proposition. Netflix's catalog is large enough that without recommendations, users can't find content they like. They browse for 60-90 seconds, give up, and eventually churn. Netflix's recommendation engine drives 75-80% of viewing hours. That means the majority of what people watch was selected by an algorithm, not by the user browsing. It's not just 'nice to have' — it's existential. Without it, churn increases and Netflix loses >$1B/year."

**Interviewer:**
Strong point. That's the kind of 'why' reasoning I'm looking for. Let's get into numbers.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists play, browse, search | Proactively raises content ingestion pipeline, multi-profile, resume, DRM per platform | Additionally discusses live streaming, ad insertion, regional licensing windows, parental controls |
| **Non-Functional** | Mentions latency and availability | Quantifies latency budget breakdown, cites specific scale numbers (301M subs, 65M concurrent), explains recommendation impact on churn | Frames NFRs in business impact: churn cost, CDN cost savings, encoding compute ROI |
| **YouTube Contrast** | Doesn't mention YouTube | Notes Netflix is curated vs YouTube UGC, different optimization targets | Explains how content model (curated vs UGC) drives every architectural choice from encoding to CDN to recommendations |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me focus on the APIs that matter most for a system design discussion — the playback critical path, catalog browsing, and content ingestion. I'll note the full API surface is broader (profiles, history, My List, admin) — documented in [02-api-contracts.md](02-api-contracts.md)."

### Playback APIs (most latency-sensitive)

> "```
> POST /playback/start
> Request:  { titleId, profileId, deviceType, drmType }
> Response: {
>     manifestUrl,        // DASH MPD or HLS M3U8 URL pointing to nearest OCA
>     drmLicenseUrl,      // License server URL for key acquisition
>     drmToken,           // Short-lived, device-bound token
>     resumePositionMs,   // Where to resume (0 if new)
>     ocaUrls: [...]      // Ranked list of OCA endpoints for failover
> }
>
> POST /playback/heartbeat  (every 30-60 seconds during playback)
> Request:  { sessionId, positionMs, bufferHealthMs, currentBitrate, selectedResolution }
> Response: { continue: true }   // Server records position for resume
>
> POST /playback/stop
> Request:  { sessionId, finalPositionMs, totalWatchedMs }
> Response: { saved: true }
> ```
>
> **Why this matters architecturally:** The `/playback/start` response is the most critical API in the entire system. It must:
> 1. Authenticate the user and check entitlements
> 2. Determine which OCAs near the user have the content cached
> 3. Generate a signed manifest URL pointing to the best OCA
> 4. Issue a DRM license token (Widevine/FairPlay/PlayReady based on device)
> 5. Look up the resume position from viewing history
>
> All of this must complete in ~100ms. That's why it's heavily cached — EVCache holds session data, catalog lookups, and OCA routing tables."

### Catalog & Recommendation APIs

> "```
> GET /recommendations/home?profileId={id}
> Response: {
>     rows: [
>         { rowTitle: 'Because you watched Stranger Things',
>           algorithm: 'because_you_watched',
>           titles: [{ titleId, title, artworkUrl, matchScore }, ...] },
>         { rowTitle: 'Trending Now',
>           algorithm: 'trending',
>           titles: [...] },
>         ...
>     ]
> }
>
> GET /catalog/titles/{titleId}
> Response: { titleId, type, synopsis, cast, genres, maturityRating,
>             episodes: [...], audioTracks, subtitleTracks, artworkVariants }
>
> GET /search?q={query}&profileId={id}
> Response: { results: [{ titleId, title, matchScore, artworkUrl }] }
> ```
>
> **Key design choice:** The home page API doesn't return 'all content' — it returns **personalized rows**, each generated by a different algorithm. The response is different for every user. The same title might show different artwork to different users based on viewing history.
>
> **Contrast with YouTube:** YouTube's equivalent API returns a single ranked feed optimized for engagement (watch time → ad impressions). Netflix returns structured rows optimized for satisfaction (reduce churn). Different business models → different API shapes."

### Content Ingestion APIs (Internal)

> "```
> POST /ingest/upload          (chunked, resumable upload of source master)
> POST /ingest/transcode       (trigger transcoding pipeline)
> GET  /ingest/jobs/{jobId}    (transcoding job status)
> POST /ingest/publish         (make transcoded assets available in catalog)
> ```
>
> The ingestion pipeline is fundamentally different from the streaming path. Netflix ingests hundreds of titles per week (not millions like YouTube). Each title goes through a DAG of encoding tasks that can take hours. Speed matters less; quality matters more. This is why Netflix can afford per-title encoding optimization — encode once, stream billions of times."

**Interviewer:**
Good. You've clearly identified the playback start as the critical path. Let's build the architecture that makes this work. Start simple and evolve.

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | Lists REST endpoints with request/response | Explains WHY each field exists (manifest URL for CDN routing, DRM token for device-binding), calls out latency constraints | Discusses API versioning strategy, backward compatibility, client-server contract evolution across 1000+ device types |
| **Playback Start** | "Returns a video URL" | Breaks down the 5-step process (auth → OCA selection → manifest → DRM → resume) with latency budget | Discusses fallback behavior (what if DRM license server is slow?), pre-fetching strategies, offline license caching |
| **YouTube Contrast** | Doesn't contrast | Notes YouTube returns a feed vs Netflix returns rows; different optimization targets | Explains how YouTube's ad insertion APIs add latency that Netflix avoids; YouTube needs VAST/VPAID integration |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me start with the simplest thing that works, find the problems, and evolve. This iterative build-up is how I'd actually think about designing this system."

---

### Attempt 0: Single Server Serving Video Files

> "Simplest possible design — one machine with videos stored as files on local disk:
>
> ```
>     User (Browser)
>         │
>         │  GET /videos/stranger-things-s1e1.mp4
>         ▼
>     ┌─────────────────────┐
>     │    Single Server     │
>     │                      │
>     │   /videos/           │
>     │     stranger-things- │
>     │     s1e1.mp4  (8 GB) │
>     │     breaking-bad-    │
>     │     s1e1.mp4  (6 GB) │
>     │                      │
>     │   Local Disk (HDD)   │
>     └─────────────────────┘
> ```
>
> User requests a video file, server sends it via HTTP."

**Interviewer:**
What's wrong with this?

**Candidate:**

> "Everything:
>
> | Problem | Impact |
> |---------|--------|
> | **One resolution** | No 4K, no mobile-friendly 240p. Every user gets the same file regardless of device or bandwidth |
> | **Huge files** | An 8 GB unoptimized file means the user downloads the entire thing before watching |
> | **No streaming** | Can't start watching until the full file downloads |
> | **Single point of failure** | Server dies = Netflix is down globally |
> | **No concurrent users** | One server can serve maybe 100 users before bandwidth saturates |
> | **No resume** | Close the browser = start from the beginning |
> | **No DRM** | Anyone can download and redistribute content |
>
> Let me fix the most fundamental problem first: encoding."

---

### Attempt 1: Upload, Transcode, and Serve from Object Storage

> "Separate the concerns: storage, transcoding, and serving.
>
> ```
>     Content Partner                              User
>         │                                          │
>         │  Upload source master                     │  GET /videos/title123/720p.mp4
>         ▼                                          ▼
>     ┌──────────────┐                        ┌──────────────┐
>     │ Upload Service│                        │  Web Server  │
>     └──────┬───────┘                        └──────┬───────┘
>            │                                       │
>            ▼                                       │
>     ┌──────────────┐                               │
>     │  Transcoding  │                               │
>     │  Workers      │                               │
>     │               │                               │
>     │  Source → 240p │                               │
>     │  Source → 480p │                               │
>     │  Source → 720p │                               │
>     │  Source → 1080p│                               │
>     └──────┬───────┘                               │
>            │                                       │
>            ▼                                       ▼
>     ┌─────────────────────────────────────────────────┐
>     │              Amazon S3 (Object Storage)          │
>     │                                                  │
>     │  /titles/title123/                               │
>     │      source.proRes          (200 GB)             │
>     │      240p.mp4               (0.3 GB)             │
>     │      480p.mp4               (0.8 GB)             │
>     │      720p.mp4               (2 GB)               │
>     │      1080p.mp4              (5 GB)               │
>     └─────────────────────────────────────────────────┘
> ```
>
> **What's better:**
> - Separated storage from compute (S3 for storage, workers for transcoding)
> - Multiple resolutions via a **fixed encoding ladder** (240p/480p/720p/1080p at fixed bitrates)
> - Content persists durably in S3
>
> **Contrast with YouTube:** YouTube's initial architecture was similar — upload → transcode → serve. But YouTube handles millions of concurrent uploads from users per day; Netflix ingests hundreds of titles per week from content partners. YouTube needs speed (transcode fast, make available quickly). Netflix needs quality (encode optimally, serve for years).
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **No adaptive streaming** | Client picks resolution upfront. If bandwidth drops mid-movie, it buffers. Can't switch quality mid-stream |
> | **Full file download** | Must download the entire 720p file before watching — no progressive streaming |
> | **Single origin** | All users worldwide fetch from one S3 region. User in Tokyo gets 200ms+ latency per request to us-east-1 |
> | **Fixed encoding ladder** | A static dialogue scene gets the same bitrate as an action scene — wasting bandwidth |
> | **No DRM** | Files are unencrypted — anyone can download and share |"

---

### Attempt 2: Adaptive Bitrate Streaming (ABR)

> "This is the fundamental shift from 'file download' to 'streaming.' Instead of downloading a complete file, we:
>
> 1. **Segment the video** — Split each resolution into 2-4 second chunks, each independently decodable (starts with a keyframe/IDR frame)
> 2. **Create a manifest** — An index file (DASH MPD or HLS M3U8) that lists all available quality levels and segment URLs
> 3. **Let the player choose** — The player downloads segments one at a time, choosing quality dynamically based on network conditions
>
> ```
>     Content Pipeline                               Player (Client)
>                                                        │
>     Source ──► Transcode ──► Segment ──► S3             │  1. GET manifest.mpd
>                                                        │  2. Parse available qualities
>     S3 Storage:                                        │  3. Download segment 1 (480p)
>     /titles/title123/                                  │  4. Measure throughput
>       manifest.mpd                                     │  5. Download segment 2 (720p)
>       480p/                                            │  6. Buffer fills → upgrade
>         seg-001.m4s (2 sec)                            │  7. Download segment 3 (1080p)
>         seg-002.m4s (2 sec)                            │  ...
>         ...                                            │
>       720p/                                            │
>         seg-001.m4s                                    │
>         seg-002.m4s                                    │
>       1080p/                                           │
>         seg-001.m4s                                    │
>         seg-002.m4s                                    │
> ```
>
> **ABR Algorithm — Netflix uses Buffer-Based Adaptation (BBA):**
>
> Most ABR algorithms use throughput estimation: measure download speed of the last N segments, pick the highest bitrate that fits. Problem: throughput estimation is noisy, causes oscillation between quality levels.
>
> Netflix's approach is **buffer-based**: the decision depends on **buffer occupancy** (how many seconds of video are buffered ahead):
> - Buffer near empty (< 5 sec) → request lowest quality (avoid stall at all costs)
> - Buffer medium (5-30 sec) → request medium quality
> - Buffer full (> 30 sec) → request highest quality the bandwidth supports
>
> BBA reduces rebuffer rate by 10-20% compared to throughput-only algorithms.
>
> **DRM (Digital Rights Management):**
> - Segments are encrypted with AES-128
> - Player obtains decryption key from a license server
> - **Widevine** (Google): Android, Chrome, smart TVs — 3 security levels (L1=hardware, L2=mixed, L3=software). L1 required for HD/4K
> - **FairPlay** (Apple): iOS, Safari, Apple TV
> - **PlayReady** (Microsoft): Windows, Xbox, some smart TVs
> - Keys are short-lived and device-bound
>
> **What's better:** Mid-stream quality switching, instant start (low quality first, ramp up), DRM protection, standard HTTP serving (no special streaming server needed).
>
> **Contrast with YouTube:** YouTube uses similar ABR (DASH primarily) but with different heuristics — YouTube optimizes for fast start with lower initial quality because user-generated content has higher abandonment rates. Netflix users are more committed (subscription model) so Netflix can afford slightly higher initial quality.
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Single origin (S3)** | All segment requests go to one AWS region. Users in Tokyo fetch segments from us-east-1 with 200ms+ round-trip per segment. At 2-sec segments, that's 10% of each segment's duration spent on network latency |
> | **Bandwidth cost** | Serving 100+ Tbps of video traffic from S3/CloudFront is prohibitively expensive at Netflix's scale |
> | **Fixed encoding ladder** | Still wasting bandwidth — simple scenes get same bitrate as complex scenes |
> | **No personalization** | Every user sees the same content in the same order on the home page |"

---

### Attempt 3: Content Delivery Network (Open Connect)

> "This is Netflix's biggest engineering achievement — a purpose-built CDN that serves 95% of all Netflix traffic.
>
> **Why build your own CDN?**
> At Netflix's scale (~15% of North American downstream internet traffic during peak), using a commercial CDN (CloudFront, Akamai) is prohibitively expensive. Back-of-envelope: 100+ Tbps of video traffic at $0.01/GB adds up to billions per year. Owning hardware amortized over 3-5 years is dramatically cheaper.
>
> ```
>     ┌─────────────────────────────────────────────────────────────────┐
>     │                        AWS (Control Plane)                      │
>     │                                                                 │
>     │  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐     │
>     │  │ Playback API │  │ OCA Steering │  │ Content Publishing │     │
>     │  │ (manifest +  │  │ (which OCA   │  │ (what content goes │     │
>     │  │  DRM + OCA   │  │  has what,   │  │  where, when)      │     │
>     │  │  selection)  │  │  health)     │  │                    │     │
>     │  └─────────────┘  └──────────────┘  └────────────────────┘     │
>     │                                                                 │
>     │  ┌──────────────────────┐                                      │
>     │  │  Origin (S3)          │ ◄── Only ~5% of traffic             │
>     │  │  Source + all encoded │                                      │
>     │  │  segments             │                                      │
>     │  └──────────────────────┘                                      │
>     └──────────────────────────┬──────────────────────────────────────┘
>                                │  Nightly proactive push
>                                │  (off-peak hours)
>                                ▼
>     ┌─────────────────────────────────────────────────────────────────┐
>     │                    Open Connect (Data Plane)                     │
>     │                                                                 │
>     │   ISP Network (Comcast)        ISP Network (NTT Japan)         │
>     │   ┌──────────────────┐         ┌──────────────────┐            │
>     │   │ OCA  OCA  OCA    │         │ OCA  OCA         │            │
>     │   │ 120TB 120TB 120TB│         │ 120TB 120TB      │            │
>     │   │ (embedded inside │         │ (embedded inside  │            │
>     │   │  ISP datacenter) │         │  ISP datacenter)  │            │
>     │   └──────────────────┘         └──────────────────┘            │
>     │                                                                 │
>     │   IXP (Internet Exchange Point)                                │
>     │   ┌──────────────────────────────┐                             │
>     │   │ OCA cluster (serves multiple │                             │
>     │   │ smaller ISPs via peering)    │                             │
>     │   └──────────────────────────────┘                             │
>     └─────────────────────────────────────────────────────────────────┘
> ```
>
> **OCA (Open Connect Appliance) hardware:**
> - **Flash OCAs**: 2U, up to 24 TB full-flash (NVMe SSDs), ~190 Gbps throughput — serves hot/popular content
> - **Storage OCAs**: 2U, up to 120 TB (HDD), ~18 Gbps throughput — stores full catalog
> - **Large deployment OCAs**: Up to 360 TB (HDD+flash mix), ~96 Gbps throughput
> - Run **FreeBSD + customized NGINX**
> - Netflix provides hardware **for free** to qualifying ISPs. ISPs provide rack space, power, connectivity. Win-win: ISP saves on transit costs, Netflix gets better delivery.
>
> **Two deployment models:**
> - **Embedded OCAs**: Installed directly inside ISP data centers. Traffic stays within the ISP's network. Used for large ISPs.
> - **IXP OCAs**: Installed at Internet Exchange Points. Serve multiple smaller ISPs via settlement-free peering.
>
> **Proactive content push (NOT reactive caching):**
>
> This is the key architectural difference from traditional CDNs:
> - Traditional CDNs (CloudFront, Akamai) use **reactive caching**: first request = cache miss → fetch from origin → cache for future requests
> - Netflix uses **proactive push**: analyze viewing patterns, predict demand, push content to OCAs during **off-peak hours** (overnight when bandwidth is cheap)
> - Content refreshed nightly based on predicted regional demand. OCAs in Japan have different content than OCAs in Brazil
> - **Result**: Cache hit ratio approaches ~100% for popular content. Only ~5% of traffic hits origin (S3)
>
> **Contrast with YouTube:** YouTube uses Google's global CDN with **reactive caching**. YouTube's long-tail content (millions of rarely-watched videos) makes proactive push infeasible — you can't predict which of 800M+ videos someone will watch next. Netflix's curated catalog (tens of thousands of titles, each watched frequently) makes proactive push practical.
>
> **URL-based client steering (not DNS-based):**
>
> Most CDNs use DNS-based routing (Anycast or geo-DNS). DNS has a TTL, so rerouting is slow (minutes). Netflix uses **URL-based steering**: the manifest contains URLs pointing directly to specific OCAs. If an OCA goes down, the very next segment request is directed to a different OCA (seconds, not minutes).
>
> **Result:** 95% of traffic served from OCAs, < 5% hits origin. Video traffic stays within the ISP's network. Lower latency, better quality, lower transit costs.
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Fixed encoding ladder** | A quiet dialogue scene in 'The Crown' gets the same bitrate as an explosion in 'Extraction'. Wasting 20-30% of bandwidth |
> | **No personalization** | Every user sees the same catalog in the same order. Discovery is broken |
> | **Single-region backend** | If the AWS region running the control plane goes down, nobody can start new streams (existing streams continue from OCAs) |
> | **No resilience testing** | We hope things work when they fail. Hope is not a strategy |"

---

### Attempt 4: Intelligence Layer (Encoding + Recommendations + Metadata)

> "Now we add the three intelligence layers that differentiate Netflix from 'just a CDN serving video files.'

#### Per-Title Encoding Optimization

> "Replace the fixed encoding ladder with a **per-title optimized ladder**:
>
> **Fixed ladder** (YouTube's approach):
> ```
> Every title gets: 240p@235kbps, 360p@560kbps, 480p@750kbps,
>                   720p@2350kbps, 1080p@5800kbps
> ```
>
> **Per-title ladder** (Netflix's approach):
> ```
> 'The Crown' S1E1 (dialogue-heavy):
>   480p@300kbps, 720p@800kbps, 1080p@2500kbps   ← lower bitrates, same quality
>
> 'Extraction' (action-heavy):
>   480p@600kbps, 720p@1800kbps, 1080p@5000kbps  ← needs more bitrate
> ```
>
> **How it works:**
> 1. Encode the title at hundreds of resolution × bitrate combinations
> 2. Measure quality of each using **VMAF** (Video Multi-Method Assessment Fusion) — Netflix's own perceptual quality metric (0-100 scale, correlates better with human perception than PSNR or SSIM)
> 3. Plot quality (VMAF) vs bitrate for each resolution
> 4. Find the **convex hull** — the Pareto-optimal points where you get the most quality per bit
> 5. The encoding ladder = the points on the convex hull
>
> **Results:**
> - Dynamic optimization reduces bitrate by ~28% for H.264, ~38% for VP9, ~34% for HEVC while retaining the same quality (measured by VMAF)
> - On 4K content: average 8 Mbps (per-title) vs 16 Mbps (fixed) = 50% bandwidth savings
> - Trade-off: ~20x more compute for encoding. Justified because Netflix encodes once, serves billions of times — compute cost is amortized
>
> **Evolution: Shot-based encoding** — Break video into shots (scene changes). Each shot gets its own optimal bitrate. A talking-head shot gets lower bitrate; an explosion gets higher. Even more efficient but even more compute-intensive.
>
> **Codecs:**
> - **H.264 (AVC)**: Universal baseline, highest bitrate for equivalent quality
> - **VP9**: ~35% more efficient than H.264, open/royalty-free (Google)
> - **AV1**: ~48% more efficient than H.264, ~25% better than VP9. **AV1 now powers 30% of Netflix streaming** (December 2025). AV1 sessions use one-third less bandwidth than AVC and HEVC, with 45% fewer buffering interruptions. On track to become Netflix's #1 codec. Netflix also launched AV1 HDR streaming in March 2025, covering ~85% of HDR catalogue
>
> **Contrast with YouTube:** YouTube cannot do per-title encoding for user-generated content (millions of uploads/day, variable quality, not worth the compute). YouTube uses fixed ladders. Netflix's curated catalog (encoded once, watched millions of times) makes per-title optimization economically viable."

#### Recommendation Engine

> "The recommendation engine is Netflix's core competitive advantage:
>
> - **75-80% of viewing hours** driven by algorithmic recommendations
> - Saves **>$1 billion/year** in reduced churn
>
> **How it works:**
> - **Collaborative filtering**: Find similar users, recommend what they watched
> - **Content-based filtering**: Use content features (genre, cast, visual style) to recommend similar content
> - **Deep learning**: Variational Autoencoders (VAE) for dense user/item embeddings, RNNs/LSTMs for sequential viewing patterns
> - **Hybrid**: Production blends all of the above
>
> **Batch + Real-time architecture:**
> - **Batch** (daily): Train models on TB of interaction data. Precompute candidate scores for all user×item pairs. Store in EVCache
> - **Real-time** (per request): Blend precomputed scores with live signals (time of day, device, what user just watched, trending content)
>
> **Personalization surfaces:**
> - Home page rows (each row generated by a different algorithm)
> - Artwork selection (different users see different thumbnails for the same title)
> - Search results ranking (same query, different user → different result order)
> - Continue watching order
>
> **Contrast with YouTube:** YouTube optimizes for **engagement** (watch time → ad impressions). Netflix optimizes for **satisfaction** (long-term retention, churn reduction). YouTube may recommend clickbait that maximizes short-term clicks; Netflix avoids this because a dissatisfied viewer cancels their subscription."

#### Metadata & Catalog Service

> "- **Content metadata**: titleId, type, synopsis (multi-language), cast/crew, genres, maturity rating, audio tracks, subtitle tracks, licensing windows (available in which countries, which dates)
> - **Search**: Powered by Elasticsearch — prefix, fuzzy, multi-language, personalized ranking
> - **Personalized artwork**: Netflix generates multiple artwork variants per title. Different users see different thumbnails based on viewing history (comedy fan sees funny artwork, action fan sees intense artwork for the same show)
>
> **What's still broken:**"

> "| Problem | Impact |
> |---------|--------|
> | **Single-region backend** | Control plane runs in one AWS region. Region outage = global outage (existing streams continue but new streams can't start) |
> | **No resilience testing** | We don't know what breaks until it breaks in production |
> | **Cascading failures** | Hundreds of tightly-coupled services. One slow dependency can bring down the entire system |
> | **No graceful degradation** | If the recommendation service is slow, the entire home page fails instead of showing a fallback |"

---

### Attempt 5: Production Hardening (Microservices + Resilience + Multi-Region)

> "This is where Netflix becomes Netflix — the resilience engineering that makes everything work at global scale.

#### Microservices Architecture (1,000+ services)

> "Netflix runs 1,000+ microservices, each owning its data and API. The Netflix OSS stack provides the infrastructure:
>
> ```
>                              ┌───────────────────────────────────┐
>                              │          Client Devices            │
>                              │  (TV, Mobile, Browser, Console)    │
>                              └───────────────┬───────────────────┘
>                                              │  HTTPS
>                              ┌───────────────▼───────────────────┐
>                              │          Zuul (API Gateway)        │
>                              │  Dynamic routing, auth, rate       │
>                              │  limiting, load balancing           │
>                              │  (Netty-based, non-blocking)       │
>                              └───────────────┬───────────────────┘
>                                              │
>                    ┌─────────────┬────────────┼────────────┬──────────────┐
>                    │             │            │            │              │
>              ┌─────▼────┐ ┌─────▼────┐ ┌─────▼────┐ ┌────▼─────┐ ┌─────▼────┐
>              │ Playback │ │ Browse/  │ │ Recommend│ │ Profile  │ │ Search   │
>              │ Service  │ │ Catalog  │ │ Service  │ │ Service  │ │ Service  │
>              └─────┬────┘ └─────┬────┘ └─────┬────┘ └────┬─────┘ └─────┬────┘
>                    │             │            │            │              │
>              ┌─────▼──────────────▼────────────▼────────────▼──────────────▼──┐
>              │                     Service Mesh Layer                          │
>              │  Eureka (Discovery)  │  Ribbon (Client LB)  │  Hystrix (CB)    │
>              └────────────────────────────────────────────────────────────────┘
>                    │             │            │            │
>              ┌─────▼────┐ ┌─────▼────┐ ┌─────▼────┐ ┌────▼─────┐
>              │ EVCache  │ │Cassandra │ │ Aurora   │ │Elastic-  │
>              │(caching) │ │(streaming│ │(billing) │ │search    │
>              │          │ │ data)    │ │          │ │(search)  │
>              └──────────┘ └──────────┘ └──────────┘ └──────────┘
> ```
>
> **Netflix OSS Stack:**
> - **Zuul** (API Gateway): Front door for all requests. Dynamic routing, authentication, rate limiting. Zuul 2 is non-blocking (Netty-based)
> - **Eureka** (Service Discovery): RESTful service registry. Services register on startup, send heartbeats. Clients cache the registry locally for client-side load balancing. No single point of failure — Eureka instances replicate peer-to-peer
> - **Ribbon** (Client-side Load Balancing): Integrates with Eureka. Round-robin, weighted response time, zone-aware routing. Runs in caller's process
> - **Hystrix** (Circuit Breaker): Prevents cascade failures. Three states: Closed (normal) → Open (failure threshold exceeded, all requests rejected with fallback) → Half-Open (testing recovery). Provides bulkhead isolation via separate thread pools per dependency. *Note: Hystrix is in maintenance mode; Resilience4j is the modern replacement*
> - **Atlas** (Telemetry): Time-series metrics platform, ingests 1+ billion metrics per minute
> - **Spinnaker** (Continuous Delivery): Canary, blue-green, rolling deployments. Open-sourced by Netflix"

#### Active-Active Multi-Region

> "Netflix runs across **4 AWS regions**, ALL serving production traffic simultaneously (active-active, NOT active-passive):
>
> ```
>     ┌──────────────────────────────────────────────────────────────┐
>     │                     Global DNS / Routing                      │
>     │              (Route users to nearest region)                  │
>     └────────┬──────────────┬──────────────┬──────────────┬────────┘
>              │              │              │              │
>     ┌────────▼───────┐ ┌───▼──────────┐ ┌▼──────────────┐ ┌▼──────────┐
>     │  US-East-1     │ │  US-West-2   │ │  EU-West-1    │ │ AP-SE-1   │
>     │  (Active)      │ │  (Active)    │ │  (Active)     │ │ (Active)  │
>     │                │ │              │ │               │ │           │
>     │  Full stack:   │ │  Full stack  │ │  Full stack   │ │ Full stack│
>     │  Zuul, APIs,   │ │              │ │               │ │           │
>     │  Cassandra,    │ │              │ │               │ │           │
>     │  EVCache,      │ │              │ │               │ │           │
>     │  Aurora        │ │              │ │               │ │           │
>     └────────────────┘ └──────────────┘ └───────────────┘ └───────────┘
>              │                 │                │                │
>              └─────── Cassandra multi-directional async replication ────┘
>              └─────── EVCache zone-aware replication ──────────────────┘
>              └─────── Aurora Global Database (<1s cross-region lag) ───┘
> ```
>
> **Why active-active over active-passive?**
> Active-passive has a dangerous assumption: the standby region works when activated. In practice, standby regions **bit-rot** — config drift, untested code paths, stale caches. Active-active eliminates this by testing every region with real production traffic continuously.
>
> **Failover:** Sub-minute — detection → rerouting → cache warming → session continuity. Each region runs at ~67% capacity to absorb failover traffic (33% headroom with 3 active regions, more with 4)."

#### Chaos Engineering

> "Netflix pioneered chaos engineering — proactively injecting failures to find weaknesses:
>
> - **Chaos Monkey**: Randomly terminates VM instances during business hours
> - **Chaos Gorilla**: Simulates failure of an entire AWS availability zone
> - **Chaos Kong**: Simulates failure of an entire AWS region
>
> Philosophy: *'The best way to avoid failure is to fail constantly.'*
>
> **Contrast with YouTube/Google:** Google has similar internal resilience testing (DiRT — Disaster Recovery Testing), but it's not as publicly documented. Netflix pioneered making chaos engineering a public practice."

#### Fallback Strategies

> "When a dependency fails, services return **degraded responses** rather than errors:
> - Recommendation service down → show generic 'Popular on Netflix' rows
> - Personalized artwork fails → show default artwork
> - Viewing history unavailable → resume from beginning instead of erroring
>
> Users see slightly less personalized content but never see an error page."

**Interviewer:**
Excellent walk-through. You've built up from a single server to Netflix's actual production architecture. Let me summarize the evolution:

---

### Architecture Evolution Table

| Attempt | Key Addition | Problem Solved | Key Technology |
|---------|-------------|---------------|----------------|
| 0 | Single server + files | Proof of concept | HTTP file serving |
| 1 | S3 + Transcoding workers + fixed ladder | Multiple resolutions, durable storage | S3, H.264, fixed encoding ladder |
| 2 | Segmented video + ABR + DRM | Mid-stream quality switching, protection | DASH/HLS, BBA algorithm, Widevine/FairPlay/PlayReady |
| 3 | Open Connect CDN + proactive push | Edge delivery, ISP-local traffic, cost | OCA hardware, URL-based steering, FreeBSD+NGINX |
| 4 | Per-title encoding + recommendations + metadata | Bandwidth efficiency, content discovery | VMAF, convex hull optimization, collaborative filtering, Elasticsearch |
| 5 | Microservices + active-active + chaos engineering | Global resilience, graceful degradation | Zuul, Eureka, Hystrix, Chaos Monkey, 4 AWS regions |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Iterative build-up** | Jumps to CDN without explaining why single server fails | Builds incrementally, each step motivated by concrete problems with the previous attempt | Additionally quantifies problems (latency numbers, bandwidth costs) to justify each evolution |
| **CDN design** | "Use a CDN like CloudFront" | Explains why Netflix built Open Connect, proactive push vs reactive caching, URL-based vs DNS-based steering | Discusses CDN economics (cost modeling), ISP relationship incentives, cache fill strategies |
| **Encoding** | "Transcode to multiple resolutions" | Explains per-title optimization, VMAF, convex hull, bandwidth savings | Discusses shot-based encoding, AV1 adoption trajectory, encoder parallelism strategies |
| **Resilience** | "Deploy to multiple regions" | Active-active with chaos engineering, fallback strategies | Discusses regional evacuation procedures, blast radius containment, shuffle sharding |
| **YouTube contrast** | Doesn't contrast | Identifies key differences (reactive vs proactive CDN, fixed vs per-title encoding) | Explains how business model (subscription vs ads) drives every architectural choice |

---

## PHASE 5: Deep Dive — Video Encoding Pipeline (~8 min)

**Interviewer:**
Let's dive deeper into the encoding pipeline. You mentioned per-title optimization — walk me through the full pipeline from source to playable content.

**Candidate:**

> "The encoding pipeline is a **DAG (Directed Acyclic Graph)** of tasks, not a linear sequence:
>
> ```
> Source Master (4K ProRes, ~200 GB)
>     │
>     ▼
> ┌─────────────────┐
> │  Video Decode    │
> └────────┬────────┘
>          │
>     ┌────┴────┐
>     ▼         ▼
> ┌────────┐ ┌────────────────┐
> │ Scene  │ │ Audio Decode   │
> │Detect  │ │ + Encode       │
> └───┬────┘ │ (AAC, EAC3,   │
>     │      │  Atmos)        │
>     ▼      └────────────────┘
> ┌──────────────────┐
> │ Per-Scene Quality│
> │ Analysis (VMAF)  │
> └───────┬──────────┘
>         │
>         ▼
> ┌───────────────────────────────────────────────┐
> │     Parallel Encode (per chunk × N profiles)   │
> │                                                 │
> │  ┌──────────┐  ┌──────────┐  ┌──────────┐     │
> │  │ H.264    │  │ VP9      │  │ AV1      │     │
> │  │ 240p-4K  │  │ 240p-4K  │  │ 240p-4K  │     │
> │  │ 10 pts   │  │ 10 pts   │  │ 10 pts   │     │
> │  └──────────┘  └──────────┘  └──────────┘     │
> │                                                 │
> │  Each encode: chunk → encode → quality measure  │
> │  → select convex hull points                    │
> └──────────────────┬──────────────────────────────┘
>                    │
>                    ▼
> ┌──────────────────────────────┐
> │  Package into fMP4 segments  │
> │  (2-4 sec per segment)       │
> │  + Generate DASH/HLS manifest│
> └──────────────┬───────────────┘
>                │
>                ▼
> ┌──────────────────────────────┐
> │  Upload to S3 + Register    │
> │  in Catalog + Push to OCAs  │
> └──────────────────────────────┘
> ```
>
> **Key facts:**
> - ~120 encoding profiles generated per title (codecs × resolutions × bitrates on the convex hull)
> - Encoding is **embarrassingly parallel**: videos are split into chunks, each chunk encoded independently across EC2 instances, then stitched
> - A 1080p title can be fully encoded in ~30 minutes via parallelized chunk-based encoding
> - Container format: **fMP4** (fragmented MP4) — each segment is a standalone fragment with its own movie fragment header (moof) + data (mdat)
>
> For the full deep dive, see [03-video-encoding-pipeline.md](03-video-encoding-pipeline.md)."

**Interviewer:**
You mentioned ~120 profiles per title. How does that compare to YouTube?

**Candidate:**

> "YouTube uses fixed encoding ladders — roughly the same set of resolutions and bitrates for every video. They can't do per-title optimization because they get 500+ hours of content uploaded per minute. The math doesn't work: per-title encoding takes 20x more compute, and YouTube's content has a long tail (most videos are watched a few times, not millions). Netflix's content is curated — tens of thousands of titles, each watched millions of times. The 20x compute cost is amortized over billions of streams."

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Pipeline** | "Transcode video to multiple resolutions" | Describes the DAG: decode → scene detect → quality analysis → parallel encode → package → publish | Discusses pipeline orchestration (Temporal/Conductor), retry semantics, idempotency of encoding tasks |
| **Per-title** | "Different titles need different bitrates" | Explains VMAF, convex hull, quantifies savings (28% for H.264, 50% for 4K) | Discusses shot-based encoding evolution, AV1 Film Grain Synthesis, encoder quality vs speed tradeoffs |
| **Scale** | "Use cloud instances" | Notes embarrassingly parallel, chunk-based encoding | Discusses spot instance strategies, encoding cost optimization, reserved vs on-demand compute tradeoffs |

---

## PHASE 6: Deep Dive — Content Delivery (Open Connect) (~8 min)

**Interviewer:**
Let's go deeper on the CDN. You mentioned proactive push — how does Netflix decide what to push where?

**Candidate:**

> "Netflix analyzes viewing patterns per region and predicts demand for each title on each OCA. The content publishing system decides:
>
> **What to push:**
> - **Popularity signals**: Historical viewing data, trending titles, seasonality
> - **New releases**: Content must be pre-positioned on ALL OCAs worldwide before the launch date (the 'hot content' problem)
> - **Regional demand**: A Korean drama is more popular on Japan/Korea OCAs than US OCAs. Bollywood content is pushed more heavily to India/UK OCAs
>
> **When to push:**
> - **Off-peak hours** (typically 2 AM - 6 AM local time): ISP bandwidth is cheapest, network is least congested
> - Content refreshed **nightly** based on updated demand predictions
>
> **Cache replacement:**
> - OCAs have finite storage (up to 360 TB for large deployments)
> - LRU-based eviction with popularity weighting
> - Netflix classifies cache misses: popularity miss (not predicted), eviction miss (storage full), new-content miss (just published)
>
> **Fill process:**
> If an OCA doesn't have requested content → fetch from a parent OCA (tier-2 at IXP) or from origin (S3). The OCA serves the client from the fetched data while simultaneously caching it for future requests.
>
> **Scale numbers:**
> - A typical large ISP deployment: 10 storage OCAs (full catalog) + 30 flash OCAs (popular content, high throughput)
> - Flash OCAs: up to 24 TB SSD, ~190 Gbps throughput
> - Storage OCAs: up to 120 TB HDD, ~18 Gbps throughput
> - Netflix has achieved **100 Gbps from a single OCA** (documented in Netflix Tech Blog)"

**Interviewer:**
What happens during a big launch — say, a new season of Stranger Things goes live at midnight?

**Candidate:**

> "The hot content problem. This is one of the most operationally complex scenarios:
>
> 1. **Days before launch**: All encoded segments are pushed to every OCA worldwide. This is coordinated by the content publishing system — it must verify every OCA has the content before the launch hour
> 2. **Launch hour**: Traffic spikes massively. The OCA steering system distributes load across all OCAs in each ISP. Flash OCAs handle the burst (they have higher throughput). Storage OCAs provide overflow capacity
> 3. **First 24 hours**: Peak traffic. If any OCA runs hot, the steering system routes traffic away from it in real-time (URL-based steering enables this — the next segment request goes to a different OCA)
> 4. **Monitoring**: Atlas tracks per-OCA metrics (CPU, disk I/O, network, error rate). Automated alerts trigger capacity rebalancing
>
> For the full deep dive, see [05-content-delivery-cdn.md](05-content-delivery-cdn.md)."

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Content push** | "Cache popular content on edge" | Explains proactive push vs reactive caching, nightly refresh, regional demand modeling | Discusses prediction model accuracy, cache miss taxonomy, fill cost optimization |
| **OCA routing** | "Route to nearest server" | URL-based steering for instant failover, OCA health tracking | Discusses BGP communities, ISP peering economics, OCA placement optimization algorithms |
| **Hot launch** | "Pre-cache the content" | Describes the multi-day coordination: pre-push → launch → monitor → rebalance | Discusses capacity modeling for launch events, A/B traffic experiments during launches |

---

## PHASE 7: Deep Dive — Data Storage & Caching (~8 min)

**Interviewer:**
Let's talk about the data layer. What databases does Netflix use and why?

**Candidate:**

> "Netflix's data layer is purpose-built for a read-heavy, globally distributed, latency-sensitive workload:
>
> | System | Use Case | Why This Choice |
> |--------|----------|----------------|
> | **Apache Cassandra** | 98% of streaming data: viewing history, bookmarks, user profiles, content metadata caches | Multi-region async replication, tunable consistency, no SPOF. Netflix tolerates brief staleness (eventual consistency) for streaming data |
> | **EVCache** (Memcached-based) | Application-level caching: session data, personalization features, homepage data, search results | 200 clusters, 22,000 instances, 400M ops/sec, 14.3 PB data, 2 trillion items. Simple KV get/set at extreme scale |
> | **Amazon Aurora** | Billing, account management, subscription state | Strong consistency needed for financial data. Aurora Global Database with <1s cross-region replication |
> | **Elasticsearch** | Search (titles, people, genres), Marken annotation service | Full-text search with fuzzy matching, multi-language support, personalized ranking |
> | **Amazon S3** | All video assets: source masters, transcoded segments, artwork, subtitles | Object storage for massive binary data. Tens of thousands of objects per title (~120 profiles × thousands of segments) |
>
> **Why Cassandra (AP) over Spanner (CP)?**
>
> Netflix chose Cassandra (available, partition-tolerant, eventually consistent) because video streaming tolerates brief staleness. A user seeing a slightly stale viewing history for a few seconds is acceptable. Strong consistency (Spanner) would add latency to every read — unacceptable for a latency-sensitive streaming service.
>
> **Contrast with YouTube:** YouTube uses Bigtable (Google's wide-column store) and Spanner (globally consistent). Similar column-family data model, but Spanner provides strong consistency (external consistency). Google can afford this because they built the hardware infrastructure (TrueTime with atomic clocks).
>
> **Why EVCache (Memcached) over Redis?**
>
> Netflix chose Memcached's simplicity for caching. Redis's rich data structures (sorted sets, lists, hyperloglogs) are unnecessary overhead for Netflix's use case — simple KV get/set. EVCache adds topology-aware replication, auto-discovery, and multi-AZ mirroring on top of Memcached's raw speed.
>
> **Multi-layer caching strategy:**
> ```
> EVCache (app-level, 400M ops/sec)
>     ↓ miss
> Cassandra (persistent, multi-region)
>     ↓ miss
> S3 (origin, video assets)
>     ↓ pushed to
> Open Connect (CDN, 95% hit rate)
>     ↓ buffered by
> Client player (30-120 sec video buffer)
> ```
>
> Each layer reduces load on the layer behind it.
>
> For the full deep dive, see [08-data-storage-and-caching.md](08-data-storage-and-caching.md)."

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Database choice** | "Use NoSQL for scale" | Explains why Cassandra (AP) over Spanner (CP), quantifies EVCache scale (400M ops/sec, 14.3 PB) | Discusses Cassandra tunable consistency levels, per-query consistency policies, read-repair strategies |
| **Caching** | "Add Redis/Memcached" | Explains multi-layer caching strategy, EVCache zone-aware replication, why Memcached over Redis | Discusses cache warming for regional failover, invalidation strategies, thundering herd mitigation |
| **Consistency** | "Eventual consistency is fine" | Separates what needs strong consistency (billing) from what tolerates eventual (viewing history) | Discusses conflict resolution for multi-region Cassandra writes, last-writer-wins semantics, anti-entropy |

---

## PHASE 8: Deep Dive — Recommendations (~5 min)

**Interviewer:**
The recommendation engine drives 75-80% of viewing. How does it work at a high level, and how does it serve at Netflix's scale?

**Candidate:**

> "The recommendation system is a **batch + real-time hybrid** architecture:
>
> **Batch pipeline (daily):**
> 1. Collect viewing data (140 million hours/day of interaction data)
> 2. Train models: collaborative filtering + deep learning (Variational Autoencoders, LSTMs for sequential patterns)
> 3. Precompute recommendation scores for all user×item candidate pairs
> 4. Store precomputed scores in EVCache
>
> **Real-time serving (per request):**
> 1. Load precomputed candidate scores from EVCache
> 2. Blend with real-time signals: current time of day, device type, what user just watched, trending content
> 3. Rank, diversify (avoid showing too many similar titles), and assemble into rows
> 4. Return personalized home page
>
> **Why this hybrid approach?**
> - Pure batch: recommendations would be stale for up to 24 hours (user watches something, recommendations don't update until next batch run)
> - Pure real-time: model training on TB of data can't happen per-request — too computationally expensive
> - Hybrid: recommendations are mostly fresh (batch scores reflect yesterday's model) with real-time adjustments (what you just watched affects scoring)
>
> **Evaluation:** A/B testing is the gold standard. Netflix runs hundreds of A/B tests concurrently. Primary metric: member retention (reduce churn). Secondary: viewing hours, title diversity, satisfaction surveys.
>
> For the full deep dive, see [07-recommendation-engine.md](07-recommendation-engine.md)."

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture** | "Use collaborative filtering" | Explains batch+real-time hybrid, why pure batch or pure real-time alone isn't sufficient | Discusses exploration-exploitation tradeoff, cold-start problem for new users/titles, multi-armed bandits |
| **Scale** | "Precompute recommendations" | Quantifies: 140M hours/day viewing data, EVCache for serving, batch retraining cadence | Discusses model serving infrastructure (TensorFlow Serving, feature stores), A/B test framework at scale |
| **Evaluation** | "A/B test it" | Explains metrics: churn reduction, viewing hours, title diversity | Discusses causal inference, long-term vs short-term metrics, novelty effects in A/B tests |

---

## PHASE 9: Wrap-Up (~3 min)

**Interviewer:**
Good. Last question: you're running this system in production. What keeps you up at night?

**Candidate:**

> "Three things:
>
> **1. Big launch day with regional failure**
> A new season of Squid Game launches globally at midnight. An hour before launch, us-east-1 starts experiencing degraded network connectivity. We need to: verify all OCAs have the content (they should — proactive push happened days ago), fail over API traffic to other regions (active-active handles this), warm EVCache in receiving regions (session data, user profiles). The nightmare scenario: content wasn't fully pushed to all OCAs, AND the origin region is degraded. Mitigation: launch readiness checks days before, not hours.
>
> **2. Cascade failure from a shared dependency**
> 1,000+ microservices create a dense dependency graph. If a low-level service (say, the user profile service) becomes slow (not down, just slow — maybe 5x normal latency), every service that calls it starts accumulating requests, filling thread pools, and eventually timing out. The cascading slowdown can bring down the entire platform. Mitigation: Hystrix circuit breakers, bulkhead isolation, aggressive timeouts, fallback responses. But circuit breakers are reactive — they open AFTER failures accumulate. The real answer is Chaos Engineering — proactively test cascade scenarios.
>
> **3. DRM key server compromise**
> If an attacker compromises the DRM license server, they could potentially extract encryption keys for Netflix's entire catalog. This is an existential content security risk. Content partners (studios) would lose confidence and pull licensing deals. Mitigation: HSMs (Hardware Security Modules), key rotation, device attestation, but the surface area is large (millions of devices, each needs keys)."

**Interviewer:**
Good awareness of operational risk. The cascade failure scenario is the most realistic — that's what actually causes Netflix incidents most often. Thanks for the thorough walk-through.

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Operational risks** | "Server failures, scaling issues" | Identifies specific scenarios (launch + regional failure, cascade from shared dependency, DRM compromise) with mitigations | Discusses organizational response: incident management runbooks, game days, blast radius containment, customer communication strategy |
| **Depth** | Lists risks | Explains root cause and mitigation for each | Proposes preventive architecture changes, discusses trade-off between resilience investment and feature velocity |

---

## Final Architecture Summary

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              CLIENT DEVICES                                   │
│              TV │ Mobile │ Browser │ Console │ Streaming Stick                │
│              (Widevine)  (FairPlay)  (PlayReady)                             │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              │  CONTROL PLANE (AWS)     │
              │  4 Active-Active Regions │
              │                          │
              │  Zuul → API Services     │
              │  Eureka (discovery)      │
              │  Hystrix (circuit break) │
              │  Atlas (1B+ metrics/min) │
              │                          │
              │  Cassandra (streaming)   │
              │  EVCache (400M ops/sec)  │
              │  Aurora (billing)        │
              │  Elasticsearch (search)  │
              │                          │
              │  Recommendation Engine   │
              │  (batch + real-time)     │
              └────────────┬────────────┘
                           │ Nightly proactive push
              ┌────────────┴────────────┐
              │  DATA PLANE             │
              │  (Open Connect)         │
              │                         │
              │  Embedded OCAs          │
              │  (inside ISP networks)  │
              │  Flash: 24TB, 190Gbps   │
              │  Storage: 120TB, 18Gbps │
              │                         │
              │  IXP OCAs               │
              │  (at exchange points)   │
              │                         │
              │  95% traffic served     │
              │  URL-based steering     │
              └─────────────────────────┘

              ┌─────────────────────────┐
              │  ENCODING PIPELINE      │
              │                         │
              │  Source → DAG pipeline  │
              │  Per-title optimization │
              │  VMAF + convex hull     │
              │  H.264/VP9/AV1 codecs  │
              │  ~120 profiles/title    │
              │  → S3 → OCAs           │
              └─────────────────────────┘
```

---

## Supporting Deep-Dive Documents

| # | Document | Topic |
|---|----------|-------|
| 1 | [01-interview-simulation.md](01-interview-simulation.md) | This file — the main interview dialogue |
| 2 | [02-api-contracts.md](02-api-contracts.md) | Comprehensive Netflix API surface |
| 3 | [03-video-encoding-pipeline.md](03-video-encoding-pipeline.md) | Transcoding DAG, codecs, per-title optimization |
| 4 | [04-adaptive-bitrate-streaming.md](04-adaptive-bitrate-streaming.md) | ABR algorithms, DASH/HLS, DRM |
| 5 | [05-content-delivery-cdn.md](05-content-delivery-cdn.md) | Open Connect architecture, OCA specs |
| 6 | [06-metadata-and-catalog.md](06-metadata-and-catalog.md) | Metadata model, search, personalized artwork |
| 7 | [07-recommendation-engine.md](07-recommendation-engine.md) | ML techniques, batch+real-time, evaluation |
| 8 | [08-data-storage-and-caching.md](08-data-storage-and-caching.md) | Cassandra, EVCache, Aurora, S3 |
| 9 | [09-microservices-and-resilience.md](09-microservices-and-resilience.md) | Netflix OSS, chaos engineering, multi-region |
| 10 | [10-scaling-and-performance.md](10-scaling-and-performance.md) | Scale numbers, latency budgets, CDN economics |
| 11 | [11-design-trade-offs.md](11-design-trade-offs.md) | Design philosophy, Netflix vs YouTube contrasts |

---

## Verified Sources

- Netflix Q4 2024 Earnings: [301.63M paid memberships](https://www.cnbc.com/2025/01/21/netflix-nflx-earnings-q4-2024.html)
- Jake Paul vs Tyson viewership: [65M concurrent streams](https://fortune.com/2024/11/16/jake-paul-vs-mike-tyson-boxing-match-65-million-viewers-netflix-streaming-glitches/)
- AV1 adoption: [30% of Netflix streaming (Dec 2025)](https://netflixtechblog.com/av1-now-powering-30-of-netflix-streaming-02f592242d80)
- Per-title encoding: [Netflix Tech Blog — Per-Title Encode Optimization](https://netflixtechblog.com/per-title-encode-optimization-7e99442b62a2)
- Open Connect appliance specs: [Netflix Open Connect](https://openconnect.netflix.com/en/appliances/)
- 100 Gbps from an OCA: [Netflix Tech Blog](https://netflixtechblog.com/serving-100-gbps-from-an-open-connect-appliance-cdb51dda3b99)
- EVCache scale: [Netflix Tech Blog — Caching for Global Netflix](https://netflixtechblog.com/caching-for-a-global-netflix-7bcc457012f1)
