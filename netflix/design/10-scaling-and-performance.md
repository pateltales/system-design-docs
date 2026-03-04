# Scaling and Performance: Cross-Cutting Concerns

This document ties together the scaling decisions across Netflix's architecture. Every design choice -- Open Connect, per-title encoding, EVCache, active-active regions -- exists because of the numbers below.

---

## 1. Scale Numbers at a Glance

| Metric | Value | Source / Date |
|--------|-------|---------------|
| Subscribers | 301M | Q4 2024 earnings report |
| Peak concurrent streams | 65M | Jake Paul vs. Tyson fight, Nov 2024 |
| Content library | Tens of thousands of titles | ~17K titles (varies by region) |
| Encoding profiles per title | ~120 | Per-title optimization pipeline |
| CDN traffic from Open Connect | ~95% | Netflix tech blog |
| Share of North American downstream internet (peak) | ~15% | Sandvine reports |
| Viewing data ingested | 140M hours/day | Netflix data engineering talks |
| EVCache total capacity | 14.3 PB across 22K instances | Netflix tech blog |
| EVCache throughput | 400M ops/sec | Netflix tech blog |
| Atlas metrics | 1+ billion metrics/minute | Netflix observability talks |

These are not aspirational targets. They are the current operating reality. Every architectural decision must hold up against these numbers.

---

## 2. Read vs. Write Asymmetry

Netflix is one of the most read-heavy systems in existence.

**The fundamental ratio:** Content is written (encoded) once and read (streamed) billions of times. The effective read:write ratio exceeds **1,000,000:1**.

This single fact drives the entire architecture:

- **Per-title encoding is justified.** Spending 20x more compute on encoding saves 20-30% bandwidth on every subsequent stream. With billions of reads, the encoding cost is negligible compared to bandwidth savings. A title that costs $1,000 extra to encode but saves $0.001 per stream pays for itself after 1M views -- and popular titles get hundreds of millions.

- **CDN investment is justified.** Building and operating a global CDN (Open Connect) makes sense because reads dominate. You optimize the read path at any cost.

- **Caching is justified at every layer.** When data is written rarely but read constantly, aggressive caching has near-perfect hit rates.

- **Consistency can be relaxed.** Since content metadata changes infrequently, eventual consistency is acceptable for most read paths. A user seeing a slightly stale catalog for a few seconds has zero impact.

Compare this to a system like a stock exchange (roughly 1:1 read:write) or a logging pipeline (write-heavy). The architecture would be fundamentally different.

---

## 3. CDN Economics: Why Open Connect Exists

### The Cost Problem with Third-Party CDNs

At Netflix's scale, using commercial CDNs (CloudFront, Akamai, Fastly) would cost billions per year.

**Back-of-envelope calculation:**

| Parameter | Value |
|-----------|-------|
| Peak bandwidth | 100+ Tbps |
| Average bandwidth (estimated) | ~40 Tbps |
| Data transferred per day | ~40 Tbps x 86,400 sec = ~432 PB/day |
| Data transferred per month | ~13 EB/month |
| Commercial CDN rate (bulk) | $0.005 - $0.01/GB |
| Monthly cost at $0.005/GB | ~$65B/month (absurd) |

Even with massive volume discounts that bring the rate down by 10x, the cost would be billions per year. The math simply does not work at this scale with third-party CDNs.

### Open Connect Economics

| Cost Component | Estimate | Amortization |
|----------------|----------|--------------|
| Custom OCA hardware (server + storage) | ~$10K-20K per appliance | 3-5 years |
| Number of OCAs globally | ~18,000+ | Across 6,000+ ISP locations |
| Total hardware investment | ~$200-400M | One-time, refreshed on cycle |
| ISP co-location agreements | Typically free (ISPs benefit from local traffic) | Ongoing |
| Annual operating cost (power, maintenance, network ops) | Estimated $200-500M/year | Ongoing |
| Total annual cost | ~$300-600M/year | Including amortized hardware |

**Savings vs. commercial CDN:** Likely 5-10x cheaper than the best negotiated third-party CDN rates. At Netflix's scale, the break-even point for building your own CDN was passed long ago.

### Why ISPs Cooperate

This is not charity. ISPs benefit enormously:

1. **Reduced transit costs.** Netflix traffic stays local instead of traversing expensive peering/transit links.
2. **Better customer experience.** Fewer buffering complaints, fewer support calls.
3. **Reduced backbone load.** 15% of downstream traffic served locally instead of from upstream.

The OCA appliances are provided to ISPs **at no cost**. Netflix pays for the hardware; the ISP provides rack space and power. Both sides win.

---

## 4. Caching Strategy: Defense in Depth

Netflix uses a multi-layer caching architecture where each layer absorbs load so the layer behind it sees reduced traffic.

```
Client Buffer (30-120 sec)
    |
    v  (cache miss = need next segments)
Open Connect OCA (CDN edge, terabytes of SSD/HDD per node)
    |
    v  (cache miss = title not on this OCA)
Open Connect Fill (regional OCAs or origin S3)
    |
    v  (cache miss = need to fetch from origin)
S3 Origin (all encoded content)
```

**For metadata and API data:**

```
Client-side cache (app memory, seconds-minutes TTL)
    |
    v
EVCache (14.3 PB, 400M ops/sec, sub-millisecond latency)
    |
    v
Cassandra / Data stores (source of truth)
```

### Layer-by-Layer Breakdown

| Layer | What It Caches | Capacity | Hit Rate | Latency |
|-------|---------------|----------|----------|---------|
| Client buffer | Next 30-120 sec of video | ~50-200 MB | N/A (pre-fetched) | 0 (already local) |
| OCA (edge) | Popular + pre-positioned content | 100-300 TB per OCA | ~95%+ for popular content | <10ms (same ISP network) |
| OCA (fill / regional) | Broader content catalog | Larger cluster capacity | Catches most remaining misses | 10-50ms (regional) |
| S3 origin | All content, all profiles | Effectively unlimited | 100% (source of truth) | 50-200ms (cross-region) |
| EVCache | Session data, user profiles, recommendations, metadata | 14.3 PB | ~99%+ | <1ms |
| Cassandra | Persistent user data, viewing history | Distributed | N/A (not a cache) | 5-20ms |

### Why This Works

The key insight: **each layer's hit rate compounds multiplicatively.**

If the client buffer absorbs 80% of segment requests, and the edge OCA handles 95% of the remainder, then the fill tier sees only 1% of total request volume. The origin sees a fraction of a percent.

This is why Netflix can serve 65M concurrent streams without melting. The origin infrastructure handles a tiny fraction of actual viewer demand.

---

## 5. Latency Budget for Playback Start

The target: **press play to first video frame in under 2 seconds.**

This is a hard engineering constraint that drives decisions across every component.

### Latency Budget Breakdown

| Step | Component | Budget | What Happens |
|------|-----------|--------|-------------- |
| 1 | DNS resolution | ~50ms | Resolve CDN hostname to nearest OCA IP |
| 2 | API call for playback manifest | ~100ms | Client requests stream URLs, available bitrates, subtitle tracks |
| 3 | DRM license acquisition | ~200ms | Widevine/FairPlay/PlayReady license fetched and validated |
| 4 | First video segment download | ~500ms | Initial segment (2-4 sec of video at lowest bitrate) from OCA |
| 5 | Decode and render first frame | ~200ms | Hardware decoder processes segment, first frame displayed |
| **Total** | | **~1,050ms** | **Leaves ~950ms of headroom against 2s target** |

### How Each Step Is Optimized

**DNS (~50ms):** Netflix uses a custom DNS system that directs clients to the optimal OCA based on real-time health, load, and network path quality. This is not simple geographic routing -- it incorporates BGP data, OCA load metrics, and historical performance.

**Manifest API (~100ms):** The playback manifest is served from the control plane (AWS), but the data is pre-computed and cached in EVCache. The API server does minimal work: authenticate, look up cache, return. No database queries in the hot path.

**DRM license (~200ms):** DRM license servers are replicated across regions. Licenses for popular content may be pre-fetched or cached on the client from previous sessions. The 200ms budget assumes a cold start.

**First segment (~500ms):** This is the largest chunk of the budget and the reason Open Connect exists. The segment must come from an OCA on the same ISP network as the viewer. At ~500ms, this allows for TCP handshake + TLS negotiation + transfer of a ~2-4 second video segment at the lowest initial bitrate (~235 kbps for the lowest profile). The client then ramps up bitrate via adaptive streaming.

**Decode + render (~200ms):** Hardware-accelerated decoding on the client device. Netflix works directly with device manufacturers (smart TVs, streaming sticks) to optimize decoder performance.

### What Eats Into Headroom

The ~950ms of headroom is not luxury. Real-world conditions degrade every step:

- Slow DNS resolvers add 50-100ms
- Congested networks increase segment download time
- Older devices have slower decoders
- DRM edge cases (expired licenses, key rotation) add round trips
- Cold TCP connections (no connection reuse) add handshake time

Netflix measures P99 playback start time, not just median. The 2-second target must hold for the vast majority of plays.

---

## 6. Encoding Compute Scaling

### Why It's Embarrassingly Parallel

Video encoding is one of the best cases for horizontal scaling:

1. **A video is split into chunks** (typically 2-4 second segments).
2. **Each chunk is encoded independently** across all target profiles.
3. **No chunk depends on any other chunk** (GOP-aligned boundaries).
4. **Results are stitched together** after all chunks complete.

For a single title with 120 profiles and, say, 1,000 chunks, that is **120,000 independent encoding jobs**. Each can run on a separate EC2 instance.

### Cost Model

| Parameter | Value |
|-----------|-------|
| Profiles per title | ~120 |
| Encoding time per profile (single machine) | Hours to days (depending on complexity) |
| EC2 instances spun up per title | Hundreds to thousands |
| Cost per title (estimated) | $5,000 - $50,000+ (depends on length, complexity, resolution) |
| Titles encoded per year | Thousands (originals + licensed content) |
| Total annual encoding compute spend | Tens of millions of dollars |

### Why the Cost Is Justified

Per-title optimization (encoding each title at bitrates tuned to its visual complexity) saves 20-30% bandwidth compared to fixed-bitrate ladders.

**Bandwidth savings calculation:**

| Metric | Value |
|--------|-------|
| Total data served per day | ~432 PB (estimated) |
| 20% bandwidth savings | ~86 PB/day saved |
| At $0.01/GB CDN cost equivalent | ~$860K/day saved |
| Annual savings | ~$314M/year |

Even at generous estimates, the bandwidth savings from per-title encoding dwarf the encoding compute cost by an order of magnitude. This is the read:write asymmetry at work.

---

## 7. Multi-Region Active-Active Scaling

Netflix runs active-active across **3 AWS regions** (us-east-1, us-west-2, eu-west-1). This is not active-passive failover. All three regions serve production traffic simultaneously.

### Capacity Planning

| Aspect | Detail |
|--------|--------|
| Regions | 3 (US-East, US-West, EU-West) |
| Traffic distribution | Geographic (users routed to nearest region) |
| Headroom per region | ~33% spare capacity |
| Failover model | Any region can absorb another's full load |
| Failover time | Minutes (DNS + health check propagation) |

### Why 33% Headroom

With 3 regions, losing 1 means the remaining 2 must absorb 100% of traffic. If traffic was evenly split (33% each), the remaining 2 regions each go from 33% to 50% load -- a 50% increase. Each region must therefore have capacity for 1.5x its normal load, which means running at ~67% utilization normally (33% headroom).

In practice, traffic is not evenly split (US-East is larger), so the headroom math is more nuanced, but the principle holds.

### Data Replication Challenges

- **Cassandra:** Multi-region replication with LOCAL_QUORUM reads and writes. Each region has a full replica. Eventual consistency across regions (typically milliseconds).
- **EVCache:** Region-local caches with asynchronous cross-region replication for critical data. Most cache data is region-specific and not replicated.
- **User state:** Viewing history, bookmarks, and My List are replicated cross-region so failover is seamless from the user's perspective.

### Zuul and Regional Routing

Zuul (Netflix's API gateway) handles regional routing:
1. DNS routes users to the nearest region.
2. If a region is unhealthy, DNS is updated to redirect traffic.
3. Zuul can also route specific requests cross-region for data locality.

---

## 8. Hot Content Problem

New releases and live events create extreme traffic spikes. The Jake Paul vs. Tyson fight hitting 65M concurrent streams is the defining example.

### The Challenge

| Scenario | Traffic Pattern |
|----------|----------------|
| Normal evening peak | Distributed across thousands of titles |
| Major original series premiere (e.g., Squid Game S2) | 10-30% of viewers on one title within hours |
| Live event (Jake Paul vs. Tyson) | 65M concurrent on a single stream |

A normal evening might see 20-30M concurrent streams spread across 5,000+ titles. A major premiere concentrates a huge fraction of that on a single title. The CDN must handle both patterns.

### Pre-Positioning Strategy

Content must be on OCAs **before** viewers press play. You cannot fill-on-demand for a title that 50M people will request simultaneously.

| Timeline | Action |
|----------|--------|
| Days before release | Content encoded in all profiles, stored in S3 |
| 48-72 hours before | Proactive push to all OCA clusters worldwide |
| 24 hours before | Verification that all OCAs have all segments for all profiles |
| Release time | DNS/manifest already points to pre-populated OCAs |
| First minutes | Monitor OCA load, rebalance if needed |

### How OCAs Handle the Spike

- **Consistent hashing** distributes segments across OCAs in a cluster so no single OCA is a hotspot.
- **SSD-based storage** handles the random read pattern of thousands of concurrent streams reading different segments.
- **OCA clusters scale horizontally** -- ISPs with more subscribers have more OCAs.
- For live events, Netflix can temporarily **increase OCA allocation** in partnership with ISPs.

### Fill Storm Prevention

Without pre-positioning, a premiere would cause a "fill storm" -- every OCA simultaneously requesting the same content from the origin, overwhelming fill tiers and S3. Pre-positioning eliminates this entirely.

---

## 9. Contrast with YouTube Scaling

Netflix and YouTube face fundamentally different scaling challenges despite both being video streaming platforms.

| Dimension | Netflix | YouTube |
|-----------|---------|---------|
| **Content volume** | Tens of thousands of titles | 800M+ videos |
| **Content source** | Centrally produced/licensed | User-generated (500 hours uploaded/minute) |
| **Viewing pattern** | Head-heavy (top titles dominate) | Extreme long tail (billions of rarely-watched videos) |
| **Primary scaling challenge** | Peak bandwidth on popular content | Storage + indexing of massive long tail |
| **Encoding strategy** | Per-title optimization (120 profiles, days of compute) | Fast, standardized encoding (must process uploads in near-real-time) |
| **CDN model** | Own CDN (Open Connect), content pre-positioned | Google's global network + edge caching; long-tail content may be served from origin |
| **Cache hit rate** | Very high (~95%+ at edge) | Lower for long-tail content; high for viral/popular videos |
| **Storage challenge** | Moderate (tens of thousands of titles x 120 profiles) | Enormous (800M+ videos, many in multiple resolutions) |
| **Search/discovery** | Recommendation-driven (no keyword search for content) | Search is core (billions of queries/day, complex ranking) |
| **Write path complexity** | Low (new content added weekly) | Extreme (500 hours/minute ingestion, transcoding, moderation, indexing) |
| **Monetization impact on architecture** | Subscription (no ad-serving latency) | Ad-supported (ad insertion adds latency, requires real-time bidding infrastructure) |

### Key Architectural Differences

**Netflix's bet:** Spend heavily on the read path (CDN, encoding optimization, caching) because content is written once and read billions of times. The write path (encoding) can be slow and expensive because it happens infrequently.

**YouTube's bet:** Optimize the write path (fast ingestion, transcoding, indexing) because 500 hours of video are uploaded every minute. The read path must handle both viral content (similar to Netflix) AND the extreme long tail (videos with <100 views that still must be stored and served).

**Why Netflix can't use YouTube's approach:** Netflix's per-title encoding (spending days to optimize each title) would be impossible for YouTube's upload volume. YouTube must use faster, less optimized encoding.

**Why YouTube can't use Netflix's approach:** Netflix's aggressive CDN pre-positioning works because the content catalog is small and predictable. YouTube cannot pre-position 800M+ videos at edge locations. Long-tail content is served from regional or origin data centers.

---

## Summary: How It All Connects

Every scaling decision at Netflix traces back to one insight: **read-heavy workloads with predictable content favor aggressive caching and CDN investment.**

```
Read:Write ratio of 1,000,000:1
    |
    +--> Per-title encoding (expensive write, cheap reads)
    |
    +--> Open Connect CDN (own the read path)
    |        |
    |        +--> Pre-positioning (predictable content catalog)
    |        |
    |        +--> ISP co-location (minimize last-mile latency)
    |
    +--> Multi-layer caching (EVCache + CDN + client buffer)
    |        |
    |        +--> Each layer reduces load on the next
    |        |
    |        +--> 400M ops/sec EVCache for metadata
    |
    +--> Active-active regions (geographic distribution + failover)
    |        |
    |        +--> 33% headroom for region failure absorption
    |
    +--> Playback start < 2 seconds (every component optimized)
```

The architecture is expensive to build but cheap to operate per stream. That is the correct trade-off when you serve 301M subscribers.
