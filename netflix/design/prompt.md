Design Netflix (Video Streaming Platform) as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/netflix/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Netflix Platform APIs

This doc should list all the major API surfaces of a Netflix-like video streaming platform. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Playback APIs**: The most critical path. `POST /playback/start` (license acquisition, DRM token, manifest URL), `POST /playback/heartbeat` (keep-alive, report buffer health, current bitrate), `POST /playback/stop` (record final position for resume), `GET /playback/resume/{titleId}` (get last position). Include DRM flow (Widevine/FairPlay/PlayReady), license server interaction, and manifest URL generation with signed tokens.

- **Catalog / Browse APIs**: `GET /catalog/titles` (paginated, filterable by genre/language/maturity), `GET /catalog/titles/{titleId}` (full metadata: synopsis, cast, episodes, artwork URLs, available audio tracks, subtitle tracks), `GET /catalog/genres` (genre taxonomy), `GET /catalog/new-releases`, `GET /catalog/trending`. Note: responses are heavily personalized — same endpoint returns different artwork, sort order, and row composition per user.

- **Recommendation / Personalization APIs**: `GET /recommendations/home` (the home page — rows of personalized content, each row is a "row algorithm" like "Because you watched X", "Trending Now", "Top 10"), `GET /recommendations/similar/{titleId}`, `GET /recommendations/continue-watching`. These are the APIs that drive 75-80% of viewing. The backend combines batch-computed model scores with real-time signals (time of day, device, recent activity).

- **Search APIs**: `GET /search?q={query}` (prefix search, fuzzy matching, multi-language), `GET /search/suggestions?q={prefix}` (typeahead autocomplete). Search covers titles, actors, directors, genres. Results are personalized (same query, different ranking per user).

- **User Profile APIs**: `GET /profiles` (list profiles under account), `POST /profiles` (create), `PUT /profiles/{profileId}` (update name, avatar, maturity settings, language), `DELETE /profiles/{profileId}`. Each account supports up to 5 profiles. Profiles have independent viewing histories, recommendations, and "My List."

- **Viewing History APIs**: `GET /history/{profileId}` (paginated viewing history), `DELETE /history/{profileId}/{titleId}` (remove from history — affects recommendations), `POST /history/{profileId}/rate` (thumbs up/down, affects recommendation model).

- **My List APIs**: `GET /mylist/{profileId}`, `POST /mylist/{profileId}/{titleId}`, `DELETE /mylist/{profileId}/{titleId}`. My List is a manually curated queue that competes with algorithmic recommendations for home page real estate.

- **Content Ingestion APIs** (internal): `POST /ingest/upload` (chunked, resumable upload of source master), `POST /ingest/transcode` (trigger transcoding pipeline — specify target profiles or use per-title optimization), `GET /ingest/jobs/{jobId}` (transcoding job status), `POST /ingest/publish` (make transcoded assets available in catalog).

- **Admin / Ops APIs** (internal): `GET /health`, `GET /metrics`, `POST /config/feature-flags`, `POST /cache/invalidate/{titleId}` (force CDN/cache refresh for a title).

**Contrast with YouTube's API model**: YouTube's public API is heavily oriented around user-generated content (upload, comment, like/dislike, subscribe, channel management). Netflix has no public upload API — content ingestion is an internal pipeline. YouTube's API must handle millions of concurrent uploads per day; Netflix ingests hundreds of titles per week. YouTube's recommendation API optimizes for engagement (watch time, ad impressions); Netflix optimizes for satisfaction (avoiding churn).

**Interview subset**: In the interview (Phase 3), focus on: playback start (the most latency-sensitive path — DRM, manifest, CDN routing), catalog browse (personalization), recommendations (the core value proposition), and content ingestion (the transcoding pipeline). The full API list lives in this doc.

### 3. 03-video-encoding-pipeline.md — Transcoding & Encoding

The encoding pipeline is the most compute-intensive part of Netflix. This doc should cover:

- **Source ingestion**: Content providers deliver source masters (often 4K ProRes or JPEG 2000 IMF packages). Files can be hundreds of GB per title. Chunked, resumable upload to S3-backed storage.
- **Transcoding DAG (Directed Acyclic Graph)**: The pipeline is a DAG of tasks, not a linear sequence. Example: source → video decode → scene detection → per-scene quality analysis → parallel encode into N resolution×codec combinations → package into segments → upload to S3 → register in catalog. Similar to a Spark/EMR DAG but specialized for media.
- **Codecs**:
  - **H.264 (AVC)**: Universal compatibility. Baseline codec for all devices. Highest bitrate for equivalent quality.
  - **VP9**: ~35% more efficient than H.264. Supported on Chrome, Android, smart TVs. Open/royalty-free (Google).
  - **AV1**: ~48% more efficient than H.264, ~25% better than VP9. AV1 powers 30% of Netflix streams and growing. Open-source (Alliance for Open Media). Trade-off: encoding is ~10-100x slower than H.264. Netflix uses hardware-accelerated AV1 encoding.
  - **Contrast with YouTube**: YouTube also uses H.264, VP9, AV1. But YouTube must transcode millions of uploads per day (user-generated), so they use fixed encoding ladders for most content — per-title optimization is infeasible at that volume.
- **Encoding ladder (resolution × bitrate profiles)**:
  - Historical fixed ladder: 235 kbps (320×240) up to 5800 kbps (1080p). Fixed regardless of content complexity.
  - ~120 encoding profiles generated per title (codecs × resolutions × bitrates).
- **Per-title encoding optimization**:
  - Each title receives a **custom bitrate ladder** tailored to its visual complexity. A static dialogue scene needs far less bitrate than an action sequence.
  - Uses **VMAF (Video Multi-Method Assessment Fusion)** — Netflix's own perceptual quality metric (0-100 scale). Correlates better with human perception than PSNR or SSIM.
  - **Convex hull optimization**: For each title, encode at hundreds of resolution×bitrate combinations. Plot quality (VMAF) vs bitrate. Find the convex hull (Pareto-optimal points). The ladder is the set of points on the hull.
  - Results: 20-30% bandwidth savings over fixed ladder. On 4K content: average 8 Mbps (per-title) vs 16 Mbps (fixed) = 50% savings.
  - Trade-off: ~20x more compute-expensive than fixed-ladder encoding. Justified because Netflix content is encoded once, served billions of times — compute cost is amortized.
  - **Contrast with YouTube**: YouTube cannot do per-title encoding for user-generated content (millions of uploads/day, variable quality, not worth the compute). YouTube uses fixed ladders. Netflix's curated catalog (tens of thousands of titles, each watched millions of times) makes per-title optimization economically viable.
- **Shot-based encoding (evolution of per-title)**: Break video into shots (scene changes). Each shot gets its own optimal bitrate. A talking-head shot gets lower bitrate; an explosion gets higher. Even more efficient but even more compute-intensive.
- **Encoding pipeline performance**: 1080p source can be encoded in ~30 minutes via parallelized chunk-based encoding. Videos are split into chunks, each chunk encoded independently across instances, then stitched.
- **Contrast with YouTube at every layer**: Volume (Netflix: hundreds of new titles/week, YouTube: 500+ hours uploaded/minute), encoding strategy (per-title vs fixed ladder), quality metric (VMAF vs simpler metrics), time pressure (Netflix: encode once, serve forever. YouTube: must be available within minutes of upload).

### 4. 04-adaptive-bitrate-streaming.md — ABR Streaming

How video gets from S3 to the user's screen without buffering.

- **Segment-based streaming**: Video is split into 2-4 second segments. Each segment is independently decodable. Each segment exists in multiple quality levels (from the encoding ladder). The player downloads segments one at a time, choosing the appropriate quality for each.
- **Manifest files**:
  - **DASH (MPEG-DASH)**: Uses MPD (Media Presentation Description) — an XML file listing all available representations (resolution, bitrate, codec), segment URLs, and timing info. Netflix's primary protocol.
  - **HLS (HTTP Live Streaming)**: Apple's protocol. Uses M3U8 playlist files. Required for iOS/Safari. Similar concept to DASH but different format.
  - Both protocols work over standard HTTP/HTTPS — no special streaming server needed. This is the key insight: video streaming is just HTTP file serving with smart client-side logic.
- **Adaptive Bitrate (ABR) algorithms**:
  - **Throughput-based**: Measure download speed of last N segments. Choose highest bitrate that fits within measured throughput. Problem: throughput estimation is noisy, leads to oscillation.
  - **Buffer-based (BBA — Buffer-Based Algorithm)**: Netflix's approach. Decision is based on **buffer occupancy** (how many seconds of video are buffered ahead). If buffer is near empty → request lowest quality (avoid stall). If buffer is healthy → request higher quality. BBA reduces rebuffer rate by 10-20% compared to throughput-only algorithms.
  - **Hybrid**: Combine throughput estimation with buffer occupancy. Most modern players use some hybrid.
  - **Contrast with YouTube**: YouTube uses a similar ABR approach but with different heuristics tuned for user-generated content (more variable quality, shorter average watch times).
- **Start-up optimization**: First few segments requested at low quality for fast start. Then ramp up quality as buffer fills. Trade-off: initial quality vs time-to-first-frame.
- **Mid-stream quality switching**: When bandwidth drops, player switches to lower quality at the next segment boundary. The switch is seamless because each segment is independently decodable (starts with a keyframe/IDR frame).
- **DRM (Digital Rights Management)**:
  - **Widevine** (Google): Android, Chrome, smart TVs. Three security levels (L1=hardware, L2=software+hardware, L3=software only). L1 required for HD/4K.
  - **FairPlay** (Apple): iOS, Safari, Apple TV.
  - **PlayReady** (Microsoft): Windows, Xbox, some smart TVs.
  - Netflix encrypts each segment with AES-128. The player obtains a decryption key from the license server after authentication. Keys are short-lived and device-bound.
  - **Contrast with YouTube**: YouTube uses Widevine primarily. YouTube Premium content has DRM; free content relies on obfuscation rather than encryption.
- **Segment format**: fMP4 (fragmented MP4) is the container format. Each segment is a standalone fMP4 fragment with its own movie fragment header (moof) + data (mdat).

### 5. 05-content-delivery-cdn.md — Open Connect & Content Delivery

Netflix's CDN is arguably the most impressive engineering feat — it's a purpose-built CDN that serves 95% of all Netflix traffic.

- **Open Connect overview**: Netflix's custom CDN. Launched 2011. Unlike using a commercial CDN (CloudFront, Akamai), Netflix builds and deploys its own hardware (OCAs — Open Connect Appliances) inside ISP networks and at Internet Exchange Points (IXPs).
- **Why build your own CDN?**
  - At Netflix's scale (15-20% of downstream internet traffic in North America during peak), using a commercial CDN is prohibitively expensive.
  - Deploying OCAs inside ISPs means video traffic never leaves the ISP's network — better quality, lower latency, and the ISP saves on peering/transit costs.
  - Netflix provides the hardware **for free** to qualifying ISPs. ISPs provide power, rack space, and connectivity. Win-win: ISP saves bandwidth costs, Netflix gets better delivery.
- **OCA (Open Connect Appliance) hardware**:
  - Storage-dense servers: up to **350 TB per appliance** (NVMe SSDs + SATA SSDs).
  - Network throughput: **9-36 Gbps** per appliance.
  - Form factor: 1U and 2U Intel Xeon-based servers. 100GbE network interfaces on modern models.
  - Run FreeBSD + NGINX (customized for streaming workloads).
- **Two deployment models**:
  - **Embedded OCAs**: Installed directly inside ISP data centers. Traffic stays entirely within the ISP's network. Ideal for large ISPs.
  - **IXP (Internet Exchange Point) OCAs**: Installed at major IXPs. Serve multiple ISPs via settlement-free peering. Used for smaller ISPs where embedded deployment isn't justified.
- **Proactive content push (NOT reactive caching)**:
  - Traditional CDNs use reactive caching: first request = cache miss → fetch from origin → cache for future requests.
  - Netflix uses **proactive push**: analyze viewing patterns, predict what users will watch, and **pre-position content on OCAs during off-peak hours** (overnight when bandwidth is cheap).
  - Content is refreshed nightly based on predicted demand. Regional popularity differs — OCAs in Japan have different content than OCAs in Brazil.
  - Result: cache hit ratio approaches 100% for popular content. Only ~5% of traffic hits the origin (S3 in AWS).
  - **Contrast with YouTube**: YouTube uses Google's global CDN with reactive caching. YouTube's long-tail content (millions of rarely-watched videos) makes proactive push infeasible. Netflix's curated catalog (smaller, every title is watched frequently) makes proactive push practical.
- **Client-to-OCA routing**:
  - Netflix's control plane (runs in AWS) maintains a mapping of: which OCAs have which content, OCA health, network proximity to client.
  - When a client requests playback, the playback API returns a manifest with URLs pointing to the **nearest healthy OCA** that has the content.
  - Steering is URL-based (not DNS-based like most CDNs). This allows instant rerouting — if an OCA goes down, the next segment request is directed to a different OCA.
  - **Contrast with YouTube/CloudFront**: Most CDNs use DNS-based routing (Anycast or geo-DNS). DNS has a TTL, so rerouting is slower (minutes). Netflix's URL-based steering can reroute on the next HTTP request (seconds).
- **Fill and cache miss handling**:
  - If an OCA doesn't have requested content → it fetches from a "parent" OCA (tier-2 cache at an IXP) or from origin (S3).
  - Netflix classifies cache misses into categories and optimizes each: popularity miss (content not yet pushed), eviction miss (storage full, LRU evicted it), new-content miss (just published).
  - Fill happens in background — the OCA serves the client from the fetched data while simultaneously caching it.
- **Scale numbers**:
  - 95% of video traffic served directly from OCAs.
  - Open Connect handles Netflix's traffic across 190+ countries.
  - Netflix accounts for ~15% of downstream internet traffic in North America during peak hours.

### 6. 06-metadata-and-catalog.md — Metadata, Search & Catalog

- **Content metadata model**: Each title has: titleId, type (movie/series/episode), synopsis (multiple languages), cast/crew, genre tags, maturity rating, available audio tracks, subtitle tracks, release date, licensing windows (available in which countries, which dates).
- **Artwork personalization**: Netflix generates multiple artwork variants per title. Different users see different artwork for the same show — personalized based on viewing history. Example: a user who watches comedies sees the funny artwork for a drama; a user who watches action sees the intense artwork.
- **Search architecture**: Powered by Elasticsearch. Handles: prefix matching, fuzzy matching (typo tolerance), multi-language search, entity search (titles, people, genres). Results are personalized — same query returns different ranking per user based on their profile.
- **Catalog service**: The central source of truth for "what content is available, where, and in what formats." Must handle regional licensing (a title may be available in the US but not Japan). Must handle windowed availability (a title may become available on a specific date).
- **Marken** (annotation service): Stores ~1.9 billion annotations on content entities. Uses Cassandra + Elasticsearch + Iceberg. Annotations include: scene boundaries, audio descriptors, content tags used for recommendation features.
- **Contrast with YouTube**: YouTube's metadata is user-generated (titles, descriptions, tags by uploaders). Netflix's metadata is professionally curated. YouTube needs spam/abuse detection on metadata; Netflix doesn't. YouTube has comments, likes, subscribe — social features that Netflix lacks.

### 7. 07-recommendation-engine.md — Recommendation & Personalization

The recommendation engine is Netflix's core competitive advantage. It drives 75-80% of viewing hours.

- **Why recommendations matter**: Netflix's catalog is large enough that users can't browse it all. Without recommendations, users give up and churn. Personalization saves Netflix >$1 billion/year in reduced churn.
- **Recommendation surfaces**:
  - **Home page rows**: Each row is generated by a different algorithm ("Because you watched X", "Trending Now", "Top 10 in your country", "New Releases"). Row selection and ordering are also personalized.
  - **Similar titles**: "More Like This" on a title's detail page.
  - **Continue watching**: Algorithmically ordered by predicted likelihood of resumption.
  - **Search results ranking**: Same query, different user → different result order.
  - **Artwork selection**: Which thumbnail to show for each title, personalized per user.
- **ML techniques**:
  - **Collaborative filtering**: User-based (find similar users) + item-based (find similar items). The classic Netflix Prize algorithm.
  - **Content-based filtering**: Use content features (genre, cast, director, visual style) to recommend similar content.
  - **Deep learning models**:
    - **Variational Autoencoders (VAE)**: Learn dense user/item embeddings from implicit feedback (what users watched, how long). Mult-VAE architecture.
    - **RNNs/LSTMs**: Model sequential viewing patterns. Captures temporal dynamics — recent history is a stronger signal than old history.
  - **Hybrid**: Production system blends all of the above.
- **Batch vs real-time**:
  - **Batch**: Model training on several terabytes of interaction data daily. Computes candidate recommendation scores for all user×item pairs (or a sampled subset). Stored in a precomputed cache.
  - **Real-time**: At request time, blend precomputed scores with real-time signals (current time of day, device type, what the user just watched, what's trending).
  - Result: recommendation latency is low (served from cache + lightweight real-time scoring), but models incorporate yesterday's data (batch retraining cadence).
- **Evaluation**: A/B testing is the gold standard. Netflix runs hundreds of A/B tests concurrently. Metrics: member retention (churn), viewing hours, title diversity, user satisfaction surveys.
- **140 million hours** of viewing data stored per day — the raw signal that feeds the recommendation models.
- **Contrast with YouTube**: YouTube optimizes for **engagement** (watch time, ad impressions, click-through rate). Netflix optimizes for **satisfaction** (long-term retention, reduced churn). This leads to different algorithmic choices: YouTube may recommend clickbait that maximizes short-term engagement; Netflix avoids this because a dissatisfied viewer cancels their subscription.

### 8. 08-data-storage-and-caching.md — Data Storage & Caching

- **Apache Cassandra**: Netflix's primary NoSQL database.
  - Stores **98% of streaming data**: user profiles, viewing history, bookmarks (resume positions), billing/payment info, content metadata caches.
  - Scale: hundreds of clusters, tens of thousands of nodes, petabytes of data, millions of transactions/second.
  - Why Cassandra? Multi-region, multi-datacenter asynchronous replication. Tunable consistency (ONE, QUORUM, ALL). No single point of failure — every node can serve reads and writes.
  - **Contrast with YouTube**: YouTube uses Bigtable (Google's proprietary wide-column store) and Spanner (globally consistent). Similar column-family data model, but Spanner provides strong consistency (external consistency) while Cassandra is eventually consistent.
- **EVCache** (Netflix's distributed caching layer):
  - Built on top of Memcached with Netflix-specific additions (topology-aware client, replication across AZs, auto-discovery).
  - Scale: **200 clusters, 22,000 Memcached instances, 400 million ops/sec, 14.3 petabytes, 2 trillion items**.
  - Data mirrored between availability zones (zone-aware replication). Sharded within zones.
  - Simple interface: get, set, touch, delete. Linear scalability.
  - Use cases: session data, personalization features, viewable catalog, homepage row data, search results.
  - **Why not Redis?** Netflix chose Memcached-based EVCache for its simplicity and raw performance for the simple KV caching use case. Redis's rich data structures are unnecessary overhead when you just need fast get/set. EVCache's topology-aware replication across AZs was custom-built for Netflix's multi-AZ requirements.
- **Amazon S3**: Object storage for all video assets.
  - Source masters (ProRes/IMF, hundreds of GB each).
  - Transcoded segments (fMP4 files, MB each, hundreds per title × ~120 profiles = tens of thousands of objects per title).
  - Artwork images, subtitle files, audio tracks.
- **Amazon Aurora**: Relational database for structured data.
  - Billing, account management, subscription state.
  - Up to 75% performance improvement vs previous MySQL setup.
- **Elasticsearch**: Search and annotation querying.
  - Powers the search experience across titles, people, genres.
  - Part of the Marken annotation service.
- **Data pipeline**: Netflix processes **140 million hours of viewing data per day**. Data flows through a pipeline of Kafka → Flink/Spark → data warehouse (Iceberg on S3) → ML training systems.
- **Contrast with YouTube's storage**: YouTube stores 800M+ videos. YouTube's storage challenge is scale of unique content (long-tail UGC). Netflix's storage challenge is encoding density (one title → ~120 profiles → thousands of segments per profile). Different bottlenecks: YouTube = metadata and index scale, Netflix = segment management and CDN fill.

### 9. 09-microservices-and-resilience.md — Microservices & Resilience

- **Microservices architecture**: Netflix runs **1,000+ microservices**. Each service owns its data and API. Evolved from a monolithic DVD-rental application.
- **Netflix OSS stack** (key components):
  - **Zuul** (API Gateway): Front door for all client requests. Dynamic routing, load balancing, authentication, rate limiting, request/response transformation. Zuul 2 is non-blocking (Netty-based).
  - **Eureka** (Service Discovery): RESTful service registry. Services register on startup, send heartbeats. Clients cache the registry locally and do client-side load balancing. No single point of failure — Eureka instances replicate peer-to-peer.
  - **Ribbon** (Client-side Load Balancing): Integrates with Eureka. Round-robin, weighted response time, zone-aware routing. Runs in the caller's process — no separate load balancer to manage.
  - **Hystrix** (Circuit Breaker): Prevents cascade failures. Three states: **Closed** (normal, requests flow) → **Open** (failure threshold exceeded, all requests rejected with fallback) → **Half-Open** (testing, limited requests allowed to probe recovery). Provides bulkhead isolation via separate thread pools per dependency. **Note**: Hystrix is in maintenance mode; Resilience4j is the modern replacement.
  - **Atlas** (Telemetry): Time-series metrics platform. Ingests **1+ billion metrics per minute**. Powers dashboards and alerting.
  - **Spinnaker** (Continuous Delivery): Multi-cloud deployment platform. Supports canary deployments, blue-green, rolling updates. Open-sourced by Netflix.
- **Chaos Engineering**:
  - **Chaos Monkey**: Randomly terminates VM instances during business hours. Forces engineers to build services that tolerate instance failure.
  - **Chaos Gorilla**: Simulates failure of an entire AWS availability zone.
  - **Chaos Kong**: Simulates failure of an entire AWS region. Tests the active-active failover.
  - Philosophy: "The best way to avoid failure is to fail constantly." Proactively inject failures to find weaknesses before they cause outages.
  - **Contrast with YouTube/Google**: Google has similar internal resilience testing (DiRT — Disaster Recovery Testing), but it's not open-sourced or as publicly documented. Netflix pioneered making chaos engineering a public practice.
- **Active-Active Multi-Region**:
  - Netflix runs across **4 AWS regions**, all serving production traffic simultaneously (active-active, NOT active-passive).
  - Every region carries real load under real conditions. No "standby" region that might fail when activated.
  - Regional failover in **sub-minute** timeframes: detection → rerouting → cache warming → session continuity.
  - **Why active-active over active-passive?** Active-passive has a dangerous assumption: the standby region works when activated. In practice, standby regions bit-rot (config drift, untested code paths, stale caches). Active-active eliminates this by testing every region continuously with real traffic.
  - Data replication: Cassandra (multi-directional async replication), EVCache (zone-aware replication), Aurora Global Database (<1 second cross-region lag).
- **Fallback strategies**: When a dependency fails, services return degraded responses rather than errors. Example: if the recommendation service is down, show a generic "Popular on Netflix" row instead of personalized recommendations. Users see slightly less personalized content but never see an error page.

### 10. 10-scaling-and-performance.md — Scaling & Performance

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers**:
  - **301 million subscribers** (Q4 2024).
  - **Peak concurrent viewership**: 65 million (Jake Paul vs Mike Tyson boxing event, 2024).
  - **Content library**: tens of thousands of titles, each encoded into ~120 profiles.
  - **CDN traffic**: 95% served from Open Connect appliances. Netflix = ~15% of North American downstream internet traffic during peak.
  - **Data**: 140 million hours of viewing data per day. EVCache: 14.3 PB across 22K instances.
- **Read vs write asymmetry**: Netflix is extremely read-heavy. Content is written (encoded) once and read (streamed) billions of times. Ratio: easily 1:1,000,000+. This asymmetry justifies per-title encoding (20x compute for 20-30% bandwidth savings on billions of reads).
- **CDN economics**: Why building Open Connect is cheaper than paying CloudFront/Akamai at Netflix's scale. Back-of-envelope: if Netflix serves 100+ Tbps of video traffic, paying a CDN $0.01/GB adds up to billions/year. Own hardware amortized over 3-5 years is dramatically cheaper.
- **Caching strategy**: Multi-layer caching. EVCache (application-level, 400M ops/sec) → CDN (Open Connect, content-level) → client-side buffer (player, 30-120 seconds of video). Each layer reduces load on the layer behind it.
- **Latency budget for playback start**: From "user presses play" to "first frame rendered" should be <2 seconds. Budget: DNS (~50ms) → API call to get manifest (~100ms) → DRM license acquisition (~200ms) → first segment download from OCA (~500ms) → decode + render (~200ms). Every component on this path is optimized for latency.
- **Encoding compute scaling**: Transcoding is embarrassingly parallel (chunks are independent). Netflix can spin up thousands of EC2 instances for a large encoding job. Compute cost per title is high but fixed — amortized over the title's lifetime of viewing.
- **Multi-region active-active scaling**: Each region handles its geographic traffic. During failure, other regions absorb the additional load. Requires headroom — each region must run at <100% capacity to absorb failover traffic. Typically ~33% headroom with 3 active regions.
- **Hot content problem**: New releases get massive traffic on day 1. OCAs must have the content pre-positioned before launch. Netflix coordinates "launch day" content push to ensure all OCAs worldwide have the title cached before it goes live.
- **Contrast with YouTube scaling**: YouTube's challenge is the long tail — millions of rarely-watched videos that must still be served. Netflix's challenge is peak load on popular content. YouTube needs massive storage and indexing; Netflix needs massive CDN bandwidth. Different scaling bottlenecks, different solutions.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of Netflix's design choices — not just "what" but "why this and not that."

- **Proactive push CDN vs reactive caching CDN**: Netflix pushes content to OCAs before it's requested. Traditional CDNs (CloudFront, Akamai) cache on first request. Proactive push works because Netflix's catalog is curated (predictable demand) and content is encoded ahead of time. Reactive caching works better for YouTube's long-tail UGC (unpredictable demand, too many titles to push everywhere).
- **Per-title encoding vs fixed encoding ladder**: Per-title is 20x more compute-expensive but saves 20-30% bandwidth on every stream. The math works for Netflix (encode once, serve billions of times). Doesn't work for YouTube (encode millions of uploads quickly, each viewed fewer times on average).
- **Own CDN (Open Connect) vs commercial CDN**: Netflix builds and operates its own CDN hardware. Most companies use CloudFront/Akamai. Netflix's traffic volume makes own CDN cheaper. Breakeven is roughly when CDN costs exceed the cost of deploying and maintaining your own hardware fleet. For most companies, that never happens — commercial CDN is cheaper and simpler.
- **Microservices vs monolith**: Netflix's 1000+ microservices enable independent deployment, scaling, and failure isolation. But microservices add operational complexity (distributed tracing, service mesh, network calls vs function calls). Netflix invested heavily in tooling (Zuul, Eureka, Hystrix, Spinnaker) to manage this complexity. For smaller companies, a monolith is often the right choice.
- **Active-active vs active-passive multi-region**: Active-active is harder to build (data replication, conflict resolution, routing complexity) but eliminates the "will the standby work?" risk. Active-passive is simpler but untested standby regions are a liability. Netflix chose active-active because their scale demands zero-downtime guarantees.
- **Chaos engineering — proactive vs reactive resilience**: Most companies fix failures after they happen. Netflix proactively causes failures (Chaos Monkey) to find weaknesses before customers do. This cultural choice requires organizational buy-in — engineers must accept that their services will be randomly killed during business hours.
- **Subscription model vs ad-supported model**: Netflix's subscription model means the optimization target is long-term satisfaction (reduce churn). YouTube's ad model means the optimization target is engagement (maximize watch time → more ad impressions). This affects every layer: recommendation algorithms, content strategy, buffering behavior, quality defaults.
- **Cassandra (AP) vs Spanner (CP)**: Netflix chose Cassandra (available, partition-tolerant, eventually consistent) because video streaming tolerates brief staleness. A user seeing a slightly stale viewing history for a few seconds is acceptable. Strong consistency would add latency to every read — unacceptable for a latency-sensitive streaming service.
- **EVCache (Memcached-based) vs Redis**: Netflix chose Memcached's simplicity for caching. Redis's rich data structures are unnecessary for Netflix's caching use case (simple KV). EVCache adds topology-aware replication, auto-discovery, and multi-AZ mirroring on top of Memcached's raw speed.

## CRITICAL: The design must be Netflix-centric
Netflix is the reference implementation. The design should reflect how Netflix actually works — its architecture, encoding pipeline, CDN (Open Connect), recommendation engine, microservices (Netflix OSS), and resilience practices. Where YouTube or other streaming services made different design choices, call those out explicitly as contrasts (e.g., "YouTube uses reactive CDN caching because its UGC long-tail makes proactive push infeasible — here's why Netflix chose proactive push").

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server serving video files from disk
- A process with videos stored as files on local disk. Client requests a video, server sends the file via HTTP.
- **Problems found**: No transcoding (only one resolution), huge files (no compression), can't handle concurrent users, single point of failure, can't resume playback.

### Attempt 1: Separate upload, transcode, and serve
- **Object storage (S3)** for video files — separates storage from compute.
- **Transcoding workers**: Take source video, produce multiple resolutions using a fixed encoding ladder (e.g., 240p/480p/720p/1080p at fixed bitrates). Each resolution is a complete file.
- Client selects a resolution at start (e.g., "720p") and downloads that file. No mid-stream switching.
- **Contrast with YouTube**: YouTube's initial architecture was similar — upload → transcode → serve. But YouTube must handle millions of concurrent uploads from users; Netflix handles hundreds of titles per week from content partners.
- **Problems found**: Client must choose resolution upfront (what if bandwidth changes?), entire file must be downloaded before playback starts (no streaming), origin server is the only source (overloaded, high latency for distant users).

### Attempt 2: Adaptive bitrate streaming
- **Segment the video**: Split each resolution into 2-4 second segments. Each segment is independently playable (starts with a keyframe).
- **Manifest file (DASH/HLS)**: Lists all available quality levels and segment URLs. Client downloads the manifest first, then fetches segments one at a time.
- **ABR algorithm**: Player monitors buffer fill level and network throughput. Selects quality for each segment dynamically. Netflix uses a **buffer-based algorithm (BBA)** — decisions based on buffer occupancy, not just throughput estimation. BBA reduces rebuffer rate by 10-20%.
- **DRM**: Encrypt segments with AES-128. Player gets decryption key from license server (Widevine for Android/Chrome, FairPlay for Apple, PlayReady for Windows). Key is device-bound and short-lived.
- **Contrast with YouTube**: Similar ABR approach (DASH primarily), but YouTube's ABR heuristics are tuned differently — YouTube optimizes for fast start (lower initial quality) because user-generated content has high abandonment rates.
- **Problems found**: Origin server still serves ALL segment requests globally. Users in Tokyo streaming from a US origin get 200ms+ latency per segment. Origin bandwidth costs are enormous.

### Attempt 3: Content delivery network (Open Connect)
- **Deploy OCAs (Open Connect Appliances)** inside ISP networks and at IXPs. Up to 350 TB storage, 9-36 Gbps throughput per appliance.
- **Proactive content push**: Analyze viewing patterns, predict demand, push content to OCAs during off-peak hours. Refresh nightly. Not reactive caching — content is pre-positioned before users request it.
- **URL-based client steering**: Control plane in AWS maps client → nearest healthy OCA with the content. Steering via URL in the manifest (not DNS). Enables instant rerouting if an OCA fails — next segment request goes to a different OCA.
- **Result**: 95% of traffic served from OCAs, <5% hits origin. Video traffic stays within the ISP's network. Lower latency, better quality, lower transit costs for the ISP.
- **Contrast with YouTube**: Google uses its own global CDN (Google Global Cache / GGC) with **reactive caching** — content is cached on first request, not proactively pushed. YouTube's long-tail content (millions of rarely-watched videos) makes proactive push impractical. Netflix's curated catalog makes it feasible.
- **Problems found**: Fixed encoding ladder wastes bandwidth (a simple dialogue scene gets the same bitrate as an action scene). No personalization — every user sees the same content in the same order. Backend runs in a single AWS region — single point of failure.

### Attempt 4: Intelligence layer (encoding optimization + recommendations + metadata)
- **Per-title encoding optimization**:
  - Replace the fixed encoding ladder with a **per-title optimized ladder**. Each title gets its own bitrate-resolution combinations based on content complexity.
  - Uses **VMAF** (Netflix's perceptual quality metric) and **convex hull optimization** to find the Pareto-optimal encoding points.
  - Results: 20-30% bandwidth savings. 4K content: 8 Mbps average (per-title) vs 16 Mbps (fixed) = 50% savings.
  - Trade-off: ~20x more compute for encoding. Justified because encode once → stream billions of times.
- **Recommendation engine**:
  - Collaborative filtering + deep learning (VAE, LSTM) → hybrid model.
  - **75-80% of viewing hours** driven by algorithmic recommendations.
  - Saves **>$1 billion/year** in reduced churn.
  - Batch training on several TB/day of interaction data. Real-time scoring blends precomputed scores with live signals.
- **Metadata & catalog service**:
  - Content taxonomy, multi-language metadata, regional licensing windows.
  - Search powered by Elasticsearch (prefix, fuzzy, multi-language, personalized ranking).
  - **Personalized artwork**: Multiple artwork variants per title. Different users see different thumbnails based on viewing history.
- **Contrast with YouTube**:
  - No per-title encoding (millions of uploads/day, compute infeasible).
  - Recommendation optimizes for engagement (watch time, ad impressions), not satisfaction (churn reduction).
  - Metadata is user-generated, not professionally curated. Requires spam/abuse detection.
- **Problems found**: Backend runs in one AWS region — if that region goes down, Netflix is down globally. No resilience testing. Hundreds of tightly-coupled services create cascading failure risk.

### Attempt 5: Production hardening (microservices + resilience + multi-region)
- **Microservices architecture (1,000+ services)**:
  - Netflix OSS stack: **Zuul** (API gateway, dynamic routing), **Eureka** (service discovery, peer-to-peer registry), **Ribbon** (client-side load balancing), **Hystrix** (circuit breaker, bulkhead isolation, fallbacks).
  - **Atlas**: telemetry platform, 1+ billion metrics per minute.
  - **Spinnaker**: continuous delivery platform (canary, blue-green, rolling).
- **Active-active multi-region**:
  - Runs across **4 AWS regions**, ALL serving production traffic simultaneously.
  - Sub-minute failover: detection → rerouting → cache warming → session continuity.
  - Not active-passive — every region is tested with real traffic continuously.
  - Data replication: Cassandra (multi-directional async), EVCache (zone-aware), Aurora Global Database (<1s lag).
- **Chaos engineering**:
  - **Chaos Monkey**: randomly kills instances during business hours.
  - **Chaos Gorilla**: simulates AZ failure.
  - **Chaos Kong**: simulates entire region failure.
  - "The best way to avoid failure is to fail constantly."
- **EVCache** (distributed caching):
  - Built on Memcached. **200 clusters, 22,000 instances, 400M ops/sec, 14.3 PB data, 2 trillion items**.
  - Zone-aware replication. Linear scalability.
- **Fallback strategies**: If recommendations service is down, show generic "Popular" rows. If personalized artwork fails, show default artwork. Users see degraded but functional experience — never an error page.
- **AV1 codec adoption**: 30% of streams now use AV1. 48% more efficient than H.264. Hardware-accelerated encoding.
- **Contrast with YouTube**: Google has similar resilience (DiRT testing) but not publicly documented like Netflix's Chaos Engineering. YouTube's scale-out is via Google's infrastructure (Borg/Kubernetes), not Netflix OSS.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Netflix internals must be verifiable against official Netflix sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Netflix Tech Blog, Netflix Research, and Open Connect documentation BEFORE writing. Search for:
   - "Netflix tech blog video encoding"
   - "Netflix Open Connect architecture"
   - "Netflix per-title encoding optimization"
   - "Netflix adaptive bitrate streaming"
   - "Netflix EVCache distributed caching"
   - "Netflix Cassandra usage at scale"
   - "Netflix recommendation system architecture"
   - "Netflix Chaos Monkey chaos engineering"
   - "Netflix active-active multi-region"
   - "Netflix Zuul API gateway"
   - "Netflix VMAF quality metric"
   - "Netflix AV1 codec adoption"
   - "Netflix microservices architecture"
   - "Netflix subscribers 2024 2025"
   - "YouTube architecture vs Netflix comparison"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to netflixtechblog.com, research.netflix.com, openconnect.netflix.com, netflix.github.io, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (subscriber count, OCA specs, EVCache scale, encoding profiles per title, CDN fill ratio), verify against Netflix Tech Blog or official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check Netflix Tech Blog]" next to it.

3. **For every claim about Netflix internals** (encoding pipeline architecture, ABR algorithm details, Open Connect routing), if it's not from an official Netflix source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse Netflix with YouTube.** These are different systems with different philosophies:
   - Netflix: curated content, subscription model, proactive CDN push, per-title encoding, optimize for satisfaction/retention
   - YouTube: user-generated content, ad-supported model, reactive CDN caching, fixed encoding ladders, optimize for engagement/watch time
   - When discussing design decisions, ALWAYS explain WHY Netflix chose its approach and how YouTube's different choice reflects a different content model and business model.

## Key Netflix topics to cover

### Requirements & Scale
- Video streaming platform with sub-2-second playback start, zero buffering at steady state
- 301M subscribers, 65M peak concurrent viewers
- Curated content library: tens of thousands of titles, each encoded into ~120 profiles
- CDN: 95% of traffic from Open Connect, ~15% of North American internet traffic
- Recommendation engine drives 75-80% of viewing, saves >$1B/year

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Single server + video files
- Attempt 1: Upload + transcode + object storage (fixed encoding ladder)
- Attempt 2: Adaptive bitrate streaming (segments, DASH/HLS, ABR, DRM)
- Attempt 3: CDN (Open Connect, proactive push, OCA deployment)
- Attempt 4: Intelligence (per-title encoding, recommendations, metadata/search)
- Attempt 5: Production hardening (microservices, chaos engineering, active-active multi-region)

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention YouTube's choice where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- Cassandra for streaming data (AP, eventual consistency, multi-region async replication)
- EVCache for application caching (Memcached-based, zone-aware)
- Aurora for relational data (billing, accounts)
- Viewing history: 140M hours/day of data, feeds recommendation models
- Eventual consistency is acceptable for most streaming data (viewing history, recommendations)
- Strong consistency needed for billing/subscription state (Aurora)

## What NOT to do
- Do NOT treat Netflix as "just a video player" — it's an end-to-end content delivery platform with encoding, CDN, recommendations, and resilience engineering. Frame it accordingly.
- Do NOT confuse Netflix with YouTube. Highlight differences at every layer, don't blur them.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against Netflix Tech Blog or mark as inferred.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
