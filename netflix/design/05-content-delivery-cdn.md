# 5. Content Delivery — Netflix Open Connect CDN

## 1. Open Connect Overview

Netflix built **Open Connect**, its own purpose-built CDN, launched in **2011**. Rather than relying on third-party CDNs (Akamai, CloudFront), Netflix designs custom hardware appliances called **Open Connect Appliances (OCAs)** and deploys them directly inside ISP networks and at Internet Exchange Points (IXPs).

Open Connect serves **~95% of all Netflix traffic**. The remaining ~5% (API calls, search, recommendations, control plane) flows through AWS.

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Netflix Architecture Split                      │
│                                                                     │
│   ┌─────────────────────────┐     ┌───────────────────────────┐     │
│   │      AWS (Control)      │     │  Open Connect (Data)      │     │
│   │                         │     │                           │     │
│   │  - API Gateway          │     │  - Video streaming        │     │
│   │  - Recommendations      │     │  - 95% of all traffic     │     │
│   │  - Search               │     │  - OCAs inside ISPs       │     │
│   │  - User profiles        │     │  - OCAs at IXPs           │     │
│   │  - Billing              │     │  - Origin: S3             │     │
│   │  - Encoding pipeline    │     │                           │     │
│   │  - OCA steering logic   │     │                           │     │
│   │                         │     │                           │     │
│   │  ~5% of traffic         │     │  ~95% of traffic          │     │
│   └─────────────────────────┘     └───────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Why Build Your Own CDN?

### The Cost Problem at Scale

Netflix accounts for **~15% of North American downstream internet traffic** during peak hours. At that scale, commercial CDN pricing is prohibitively expensive.

**Back-of-envelope calculation:**

```
Netflix peak throughput:    ~100+ Tbps globally
Assume average utilization: 40 Tbps sustained

Data per day:  40 Tbps × 86,400 sec = 432 PB/day
Data per year: 432 PB × 365 = ~157 EB/year = 157,000 PB/year

Commercial CDN rate (bulk): ~$0.005 - $0.01 per GB

At $0.005/GB:
  157,000 PB × 1,000,000 GB/PB × $0.005 = $785 billion/year  ← absurd

Even at $0.001/GB (extreme discount):
  157,000,000,000 GB × $0.001 = $157 billion/year              ← still absurd

Netflix annual revenue: ~$33 billion (2023)
```

The numbers don't work at any commercial CDN price point. Building your own is the only viable option.

**Own hardware economics:**

```
OCA unit cost (estimated): $10,000 - $20,000
Deployed globally:         ~18,000+ OCAs (estimated)
Total hardware cost:       ~$180M - $360M
Amortized over 5 years:    ~$36M - $72M/year
Add operations/support:    ~$100M - $200M/year total

vs. commercial CDN:        billions/year
Savings:                   orders of magnitude
```

### The ISP Incentive

Netflix doesn't just save money — ISPs **want** this arrangement:

```
WITHOUT Open Connect:                   WITH Open Connect:

  Netflix Origin (AWS)                    Netflix Origin (AWS)
        │                                       │
        │ (crosses peering/transit)              │ (nightly fill, off-peak)
        ▼                                       ▼
  ┌──────────┐                            ┌──────────┐
  │ Transit  │                            │  IXP OCA │ (backup)
  │ Provider │                            └──────────┘
  └──────────┘
        │                                 ┌──────────────────────┐
        │ (ISP pays for transit)          │   ISP Network         │
        ▼                                 │                      │
  ┌──────────────────────┐                │   ┌──────────┐       │
  │   ISP Network         │                │   │ OCA      │       │
  │                      │                │   │ (inside) │       │
  │   Subscribers ◄──────┤                │   └────┬─────┘       │
  │                      │                │        │             │
  └──────────────────────┘                │   Subscribers ◄──────┤
                                          │   (traffic stays     │
  ISP pays transit costs for              │    inside network)   │
  massive Netflix traffic                 └──────────────────────┘

                                          ISP saves on transit/peering
                                          Lower latency for users
                                          Netflix provides hardware FREE
```

Netflix provides OCAs to ISPs **at no cost** — the ISP provides rack space, power, and network connectivity.

---

## 3. OCA Hardware

Netflix publishes hardware specs at [openconnect.netflix.com](https://openconnect.netflix.com). The appliances are purpose-built for one thing: serving video bytes as fast as possible.

### Flash OCAs (Hot Content)

```
┌──────────────────────────────────────────────────────┐
│  Flash OCA — 2U Chassis                              │
│                                                      │
│  Storage:     Up to 24 TB NVMe SSDs (full-flash)     │
│  Throughput:  ~190 Gbps from a single server          │
│  Power:       ~400W peak                              │
│  Use case:    Popular / trending content              │
│                                                      │
│  Key insight: NVMe removes disk I/O bottleneck.      │
│  At 190 Gbps, the NIC is the bottleneck, not disk.   │
└──────────────────────────────────────────────────────┘
```

### Storage OCAs (Full Catalog)

```
┌──────────────────────────────────────────────────────┐
│  Storage OCA — 2U Chassis                            │
│                                                      │
│  Storage:     Up to 120 TB HDD                       │
│  Throughput:  ~18 Gbps                                │
│  Power:       ~270W peak                              │
│  Use case:    Full catalog / long-tail content        │
│                                                      │
│  Key insight: HDD seek times limit throughput.        │
│  Cheap per-TB, stores entire catalog at each site.    │
└──────────────────────────────────────────────────────┘
```

### Large Deployment (Hybrid)

```
┌──────────────────────────────────────────────────────┐
│  Large Deployment OCA                                │
│                                                      │
│  Storage:     Up to 360 TB (HDD + flash mix)         │
│  Networking:  6 × 10 GbE  or  2 × 100 GbE           │
│  Throughput:  ~96 Gbps                                │
│  Use case:    Large ISP embedded deployments          │
└──────────────────────────────────────────────────────┘
```

### Software Stack

```
┌─────────────────────────────────────────┐
│            OCA Software Stack           │
├─────────────────────────────────────────┤
│  Application:  Customized NGINX         │
│  OS:           FreeBSD                  │
│  Networking:   Custom kernel TCP stack  │
│  Storage I/O:  Async I/O, sendfile()    │
│  TLS:          Hardware-accelerated     │
└─────────────────────────────────────────┘

Why FreeBSD?
- Superior networking stack (sendfile, kqueue)
- Netflix engineers are top FreeBSD contributors
- Custom kernel patches for zero-copy TLS
- Achieved 100 Gbps from a single OCA
  (documented on netflixtechblog.com)
```

### Typical Large ISP Deployment

```
┌─────────────────────────────────────────────────────────┐
│              Large ISP Data Center                       │
│                                                         │
│   Storage OCAs (full catalog):                          │
│   ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐             │
│   │ S-1 │ │ S-2 │ │ S-3 │ │ S-4 │ │ S-5 │             │
│   └─────┘ └─────┘ └─────┘ └─────┘ └─────┘             │
│   ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐             │
│   │ S-6 │ │ S-7 │ │ S-8 │ │ S-9 │ │S-10 │             │
│   └─────┘ └─────┘ └─────┘ └─────┘ └─────┘             │
│                                                         │
│   Flash OCAs (popular content):                         │
│   ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐     │
│   │ F-1 │ │ F-2 │ │ F-3 │ │ F-4 │ │ F-5 │ │ F-6 │     │
│   └─────┘ └─────┘ └─────┘ └─────┘ └─────┘ └─────┘     │
│   ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐     │
│   │ F-7 │ │ F-8 │ │ F-9 │ │F-10 │ │F-11 │ │F-12 │     │
│   └─────┘ └─────┘ └─────┘ └─────┘ └─────┘ └─────┘     │
│   ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐     │
│   │F-13 │ │F-14 │ │F-15 │ │F-16 │ │F-17 │ │F-18 │     │
│   └─────┘ └─────┘ └─────┘ └─────┘ └─────┘ └─────┘     │
│   ... (up to ~30 flash OCAs)                            │
│                                                         │
│   Total: ~10 storage + ~30 flash OCAs                   │
│   Aggregate throughput: ~6+ Tbps per site               │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Two Deployment Models

### Model 1: Embedded (Inside ISP)

OCAs deployed directly within the ISP's own data center. Traffic never leaves the ISP's network.

```
┌────────────────────────────────────────────────────────────┐
│                     ISP Network                            │
│                                                            │
│   ┌─────────────┐         ┌──────────────────────┐         │
│   │   ISP Edge   │         │  ISP Data Center      │         │
│   │   Router     │◄────────┤                      │         │
│   └──────┬──────┘         │   ┌──────┐ ┌──────┐  │         │
│          │                │   │ OCA  │ │ OCA  │  │         │
│          │                │   │  #1  │ │  #2  │  │         │
│          │                │   └──────┘ └──────┘  │         │
│          ▼                │   ┌──────┐ ┌──────┐  │         │
│   ┌──────────────┐        │   │ OCA  │ │ OCA  │  │         │
│   │  Subscriber   │        │   │  #3  │ │  #4  │  │         │
│   │  Homes        │        │   └──────┘ └──────┘  │         │
│   │  ┌──┐ ┌──┐   │        └──────────────────────┘         │
│   │  │TV│ │TV│   │                                         │
│   │  └──┘ └──┘   │        Traffic path: OCA → ISP router   │
│   └──────────────┘        → subscriber. Never leaves ISP.  │
│                                                            │
└────────────────────────────────────────────────────────────┘

Benefits:
  - Lowest latency (1-2 hops to subscriber)
  - Zero transit cost for ISP
  - Highest quality streams
  - ISP provides: rack space, power, connectivity
  - Netflix provides: hardware, software, content, support
```

### Model 2: IXP (Internet Exchange Point)

OCAs deployed at IXPs, serving multiple smaller ISPs that peer at that exchange.

```
┌──────────────────────────────────────────────────────────────┐
│                   Internet Exchange Point                     │
│                                                              │
│    ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐             │
│    │ OCA  │ │ OCA  │ │ OCA  │ │ OCA  │ │ OCA  │             │
│    │  #1  │ │  #2  │ │  #3  │ │  #4  │ │  #5  │             │
│    └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘             │
│       └────────┴────────┼────────┴────────┘                  │
│                         │                                    │
│              ┌──────────┴──────────┐                         │
│              │   IXP Switch Fabric  │                         │
│              └──┬─────┬──────┬────┘                          │
│                 │     │      │                                │
└─────────────────┼─────┼──────┼───────────────────────────────┘
                  │     │      │
         ┌────────┘     │      └────────┐
         ▼              ▼               ▼
   ┌──────────┐  ┌──────────┐    ┌──────────┐
   │  ISP A   │  │  ISP B   │    │  ISP C   │
   │ (small)  │  │ (small)  │    │ (medium) │
   │          │  │          │    │          │
   │ 50K subs │  │ 30K subs │    │ 200K subs│
   └──────────┘  └──────────┘    └──────────┘

Benefits:
  - Serves multiple ISPs from one location
  - ISPs too small for embedded deployment still benefit
  - Peering = no transit cost
  - Slightly higher latency than embedded (extra hop)
```

### Deployment Decision Matrix

```
ISP Size            Deployment        Reason
─────────────────────────────────────────────────
Large (>1M subs)    Embedded          Volume justifies dedicated hardware
Medium (100K-1M)    Embedded or IXP   Depends on ISP willingness
Small (<100K)       IXP               Shared deployment, lower overhead
Remote/rural        IXP or none       Served from nearest available OCA
```

---

## 5. Proactive Content Push (NOT Reactive Caching)

This is the single most important architectural distinction between Netflix's CDN and traditional CDNs.

### Traditional CDN: Reactive Caching

```
Traditional CDN (Akamai, CloudFront):

  Client                CDN Edge              Origin
    │                      │                     │
    │── GET /video.mp4 ───▶│                     │
    │                      │ CACHE MISS          │
    │                      │── fetch ────────────▶│
    │                      │◄── video data ──────│
    │◄── video data ───────│                     │
    │                      │ (now cached)        │
    │                      │                     │
    │                      │                     │
  Client 2                 │                     │
    │── GET /video.mp4 ───▶│                     │
    │                      │ CACHE HIT           │
    │◄── video data ───────│                     │

  Problem: First viewer in a region gets a cache miss.
  Cold starts degrade experience.
  Popular content eventually cached, but long-tail = misses.
```

### Netflix Open Connect: Proactive Push

```
Netflix: Proactive push during off-peak hours

  ┌───────────────────────────────────────────────────────────────┐
  │                    Nightly Fill Process                        │
  │                                                               │
  │   AWS Control Plane                                           │
  │   ┌──────────────────────────────────────────────────┐        │
  │   │  1. Analyze regional viewing patterns             │        │
  │   │  2. Predict tomorrow's demand per region          │        │
  │   │  3. Determine which encodes to push where         │        │
  │   │  4. Factor in OCA storage capacity                │        │
  │   │  5. Generate fill plan                            │        │
  │   └──────────────────────┬───────────────────────────┘        │
  │                          │                                    │
  │              Off-peak hours (2 AM - 8 AM local)               │
  │                          │                                    │
  │           ┌──────────────┼──────────────┐                     │
  │           ▼              ▼              ▼                     │
  │     ┌──────────┐  ┌──────────┐  ┌──────────┐                 │
  │     │ OCA Site │  │ OCA Site │  │ OCA Site │                 │
  │     │  India   │  │  Brazil  │  │   USA    │                 │
  │     │          │  │          │  │          │                 │
  │     │ Bollywood│  │ Novelas  │  │ US Top50 │                 │
  │     │ trending │  │ trending │  │ trending │                 │
  │     │ + global │  │ + global │  │ + global │                 │
  │     │   hits   │  │   hits   │  │   hits   │                 │
  │     └──────────┘  └──────────┘  └──────────┘                 │
  │                                                               │
  └───────────────────────────────────────────────────────────────┘

  Result: When a subscriber hits play at 8 PM,
  the content is ALREADY on the nearest OCA.
  Cache hit ratio ≈ 100% for popular content.
  Only ~5% of requests ever reach origin (S3).
```

### Why This Works for Netflix but Not YouTube

```
Netflix vs YouTube: Content Distribution Model

┌─────────────────────────────┬────────────────────────────────┐
│         Netflix             │          YouTube               │
├─────────────────────────────┼────────────────────────────────┤
│ Catalog: ~15,000 titles     │ Catalog: 800M+ videos          │
│ Content: professional       │ Content: user-generated (UGC)  │
│ Upload rate: ~100/week      │ Upload rate: 500 hrs/minute     │
│ Viewership: concentrated    │ Viewership: extreme long-tail  │
│ Predictable demand          │ Unpredictable virality         │
│                             │                                │
│ Strategy: PROACTIVE PUSH    │ Strategy: REACTIVE CACHING     │
│                             │                                │
│ Push all popular content    │ Cannot push 800M+ videos       │
│ to all OCA sites nightly    │ to all edge locations           │
│                             │                                │
│ Cache hit ≈ 100%            │ Cache hit varies (60-90%)      │
│ Only ~5% hits origin        │ Origin serves long-tail        │
└─────────────────────────────┴────────────────────────────────┘

Key insight: Netflix's catalog is small enough AND predictable
enough that proactive push is feasible. YouTube's isn't.
```

### What Gets Pushed Where

```
Content Tiering on OCAs:

┌─────────────────────────────────────────────────┐
│               Flash OCAs (24 TB)                │
│                                                 │
│  Tier 1: Top 50 titles in this region           │
│          All bitrate encodes (4K, 1080p, etc.)  │
│          Represents ~80% of views               │
│          Refreshed nightly                       │
│                                                 │
│  Result: 80% of requests served at NVMe speed   │
│          (190 Gbps per OCA)                      │
└─────────────────────────────────────────────────┘
                     │
                     │ overflow
                     ▼
┌─────────────────────────────────────────────────┐
│             Storage OCAs (120 TB)               │
│                                                 │
│  Tier 2: Full catalog for this region           │
│          All titles, all encodes                 │
│          Represents remaining ~20% of views      │
│          Updated less frequently                 │
│                                                 │
│  Result: Long-tail served at HDD speed          │
│          (18 Gbps per OCA, still fast enough)    │
└─────────────────────────────────────────────────┘
                     │
                     │ rare miss (~5%)
                     ▼
┌─────────────────────────────────────────────────┐
│            Origin (AWS S3)                      │
│                                                 │
│  Tier 3: Complete master catalog                │
│          All encodes, all regions                │
│          Source of truth for fills               │
│                                                 │
│  Only hit for: brand-new content before fill,   │
│  extremely rare titles, or OCA failure           │
└─────────────────────────────────────────────────┘
```

---

## 6. Client-to-OCA Routing (URL-Based Steering)

### How Most CDNs Route: DNS-Based

```
Traditional CDN Routing (CloudFront / Akamai):

  Client                     DNS                      CDN Edge
    │                         │                          │
    │── DNS query ───────────▶│                          │
    │   cdn.example.com       │                          │
    │                         │ Geo-DNS / Anycast        │
    │                         │ Resolve to nearest       │
    │                         │ edge IP                  │
    │◄── IP: 203.0.113.50 ───│                          │
    │                         │                          │
    │── HTTPS GET ───────────────────────────────────────▶│
    │                                                    │ (edge-50)
    │◄── video data ─────────────────────────────────────│
    │                                                    │

  Problem: DNS has TTL (30s - 300s).
  If edge-50 goes down, client keeps using cached DNS
  for up to TTL duration. Rerouting takes MINUTES.

  Also: DNS resolvers aggregate clients, so geo-mapping
  is imprecise (resolver ≠ client location).
```

### How Netflix Routes: URL-Based Steering

```
Netflix URL-Based Steering:

  Step 1: Client requests playback session from AWS control plane

  Client App              AWS (Play API)            OCA Health DB
     │                         │                         │
     │── "I want to watch     │                         │
     │    Stranger Things" ──▶│                         │
     │                         │── Query: which OCAs     │
     │                         │   have this content     │
     │                         │   near this client? ───▶│
     │                         │                         │
     │                         │◄── OCA-7 (ISP-local)   │
     │                         │    OCA-12 (IXP backup)  │
     │                         │    OCA-22 (failover)    │
     │                         │                         │
     │◄── Manifest with URLs: │                         │
     │    primary:   https://oca7.nflx.net/seg1.mp4     │
     │    fallback1: https://oca12.nflx.net/seg1.mp4    │
     │    fallback2: https://oca22.nflx.net/seg1.mp4    │

  Step 2: Client fetches segments directly from OCA

  Client App              OCA-7 (ISP-local)
     │                         │
     │── GET /seg1.mp4 ───────▶│
     │◄── video data ─────────│
     │── GET /seg2.mp4 ───────▶│
     │◄── video data ─────────│
     │── GET /seg3.mp4 ───────▶│
     │◄── video data ─────────│
     │                         │
     │   (continues...)        │

  Step 3: If OCA-7 fails, client switches on NEXT request

  Client App              OCA-7              OCA-12 (fallback)
     │                      │                       │
     │── GET /seg4.mp4 ────▶│                       │
     │◄── timeout/error ────│                       │
     │                      │                       │
     │── GET /seg4.mp4 ─────────────────────────────▶│
     │◄── video data ──────────────────────────────│
     │── GET /seg5.mp4 ─────────────────────────────▶│
     │◄── video data ──────────────────────────────│

  Failover time: ~1 HTTP request timeout (seconds, not minutes)
```

### DNS vs URL-Based Steering Comparison

```
┌──────────────────────────┬─────────────────────────────────┐
│   DNS-Based (Akamai)     │   URL-Based (Netflix)           │
├──────────────────────────┼─────────────────────────────────┤
│ Reroute time: TTL        │ Reroute time: next HTTP req     │
│ (30s - 300s)             │ (~2-5 seconds)                  │
│                          │                                 │
│ Granularity: per-domain  │ Granularity: per-request        │
│                          │                                 │
│ Client location: DNS     │ Client location: IP from API    │
│ resolver IP (imprecise)  │ call (precise)                  │
│                          │                                 │
│ Health check: DNS TTL    │ Health check: real-time from    │
│ refresh cycle            │ OCA heartbeats to control plane │
│                          │                                 │
│ Content awareness: no    │ Content awareness: yes          │
│ (DNS doesn't know what   │ (steer to OCA that HAS the     │
│  content is on which     │  specific file)                 │
│  edge)                   │                                 │
│                          │                                 │
│ A/B testing: hard        │ A/B testing: trivial            │
│                          │ (different URLs per client)     │
└──────────────────────────┴─────────────────────────────────┘
```

### Full Steering Flow Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Complete Playback Flow                           │
│                                                                      │
│  ┌──────────┐     ┌─────────────────────────────────────────────┐    │
│  │  Client   │     │              AWS Control Plane              │    │
│  │  (TV app) │     │                                             │    │
│  └─────┬────┘     │  ┌──────────┐  ┌────────┐  ┌───────────┐   │    │
│        │          │  │ Play API │  │Steering│  │OCA Health │   │    │
│        │          │  │          │  │ Service│  │  Monitor  │   │    │
│        │          │  └──────────┘  └────────┘  └───────────┘   │    │
│        │          └─────────────────────────────────────────────┘    │
│        │                                                             │
│        │  1. POST /play {titleId, profileId}                        │
│        │─────────────────────────▶ Play API                         │
│        │                              │                              │
│        │                    2. Query steering service:               │
│        │                       - Client IP → ISP, region            │
│        │                       - Which OCAs in that ISP?            │
│        │                       - Which have this title's encodes?   │
│        │                       - OCA health / load?                 │
│        │                       - Client bandwidth estimate?         │
│        │                              │                              │
│        │                    3. Rank OCAs:                            │
│        │                       - Prefer embedded (same ISP)         │
│        │                       - Then IXP (same metro)              │
│        │                       - Then remote (failover)             │
│        │                              │                              │
│        │  4. Response: manifest with ranked OCA URLs                │
│        │◄─────────────────────────────┘                             │
│        │                                                             │
│        │  5. GET segment from primary OCA                           │
│        │──────────────────────────────────────▶ OCA-7 (ISP)         │
│        │◄──────────────────────────────────────                     │
│        │                                                             │
│        │  6. Periodic: report quality metrics to AWS                │
│        │─────────────────────────▶ Telemetry                        │
│        │                          (used to update steering)         │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 7. Fill and Cache Miss Handling

Even with proactive push, cache misses can occur. Netflix handles them with a tiered fill hierarchy.

### Cache Miss Categories

```
┌──────────────────────┬──────────────────────────────────────────┐
│ Miss Type            │ Cause                                    │
├──────────────────────┼──────────────────────────────────────────┤
│ New-content miss     │ Title just released, nightly fill hasn't │
│                      │ run yet. Rare — Netflix pre-positions    │
│                      │ new releases before launch.              │
├──────────────────────┼──────────────────────────────────────────┤
│ Popularity miss      │ Title not popular enough in this region  │
│                      │ to justify pre-positioning on flash OCAs.│
│                      │ Falls through to storage OCA or origin.  │
├──────────────────────┼──────────────────────────────────────────┤
│ Eviction miss        │ OCA disk full, LRU evicted older encode. │
│                      │ User requests that specific encode.      │
├──────────────────────┼──────────────────────────────────────────┤
│ Failure miss         │ OCA hardware failure, content on failed  │
│                      │ disk. Steering redirects to another OCA. │
└──────────────────────┴──────────────────────────────────────────┘
```

### Fill Hierarchy

```
Cache Miss Fill Path:

  Client request
       │
       ▼
  ┌──────────────┐   HIT
  │  Flash OCA   │──────────▶ Serve directly
  │  (embedded)  │            (190 Gbps, <1ms seek)
  └──────┬───────┘
         │ MISS
         ▼
  ┌──────────────┐   HIT
  │ Storage OCA  │──────────▶ Serve directly
  │  (embedded)  │            (18 Gbps, HDD seek)
  └──────┬───────┘
         │ MISS
         ▼
  ┌──────────────┐   HIT
  │   IXP OCA    │──────────▶ Serve to client
  │  (parent)    │            + async cache to embedded OCA
  └──────┬───────┘
         │ MISS
         ▼
  ┌──────────────┐
  │  Origin (S3) │──────────▶ Serve to client
  │  in AWS      │            + async cache to IXP + embedded
  └──────────────┘

  Key behavior: "Serve while caching"
  The OCA serves the client immediately while simultaneously
  caching the content for future requests (no store-then-serve delay).
```

### Fill During Off-Peak vs On-Demand Fill

```
┌─────────────────────────────────────────────────────────────────┐
│                     Two Fill Mechanisms                          │
│                                                                 │
│   ┌─────────────────────────────┐                               │
│   │  Proactive Fill (Nightly)   │                               │
│   │                             │                               │
│   │  Scheduled: 2 AM - 8 AM    │                               │
│   │  Direction: Origin → OCAs  │                               │
│   │  Scope: predicted popular  │                               │
│   │  Bandwidth: uses spare     │                               │
│   │  capacity during off-peak  │                               │
│   │  Coverage: ~95% of views   │                               │
│   └─────────────────────────────┘                               │
│                                                                 │
│   ┌─────────────────────────────┐                               │
│   │  Reactive Fill (On-demand)  │                               │
│   │                             │                               │
│   │  Triggered: cache miss     │                               │
│   │  Direction: parent → child │                               │
│   │  Scope: specific file      │                               │
│   │  Bandwidth: on-demand      │                               │
│   │  Coverage: ~5% of views    │                               │
│   │  Behavior: serve + cache   │                               │
│   └─────────────────────────────┘                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 8. Scale Numbers

```
┌───────────────────────────────────────────────────────────────┐
│                Netflix Open Connect at Scale                  │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  Traffic share:        ~95% served from OCAs                  │
│                        ~5% from AWS origin                    │
│                                                               │
│  Global reach:         190+ countries                         │
│                                                               │
│  Internet share:       ~15% of North American downstream      │
│                        traffic during peak hours              │
│                                                               │
│  Peak throughput:      100+ Tbps globally (estimated)         │
│                                                               │
│  Single OCA record:    100 Gbps from one server               │
│                                                               │
│  ISP partners:         1,000+ ISPs and IXPs worldwide         │
│                                                               │
│  Content pushed:       Refreshed nightly per region            │
│                        Region-specific popularity rankings    │
│                                                               │
│  Encoding variants:    Each title encoded in ~1,200+ files    │
│                        (bitrates × resolutions × codecs       │
│                         × audio tracks × subtitles)           │
│                                                               │
│  Catalog:              ~15,000+ titles                        │
│                        ~100+ PB total encoded content         │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

---

## Interview Talking Points

**"Why did Netflix build their own CDN?"**
> At 15% of North American downstream traffic, commercial CDN costs would exceed their revenue. Building custom hardware (OCAs) deployed inside ISP networks amortizes to a fraction of the cost. ISPs benefit too — traffic stays local, saving them transit costs. Netflix provides the hardware for free.

**"How is Netflix's CDN different from a traditional CDN?"**
> Two key differences: (1) Proactive push vs reactive caching — Netflix pushes predicted popular content to OCAs overnight, so cache hit rate approaches 100%. Traditional CDNs cache on first miss. (2) URL-based steering vs DNS-based routing — Netflix embeds the specific OCA address in the video manifest URL, enabling per-request failover in seconds. DNS-based CDNs are limited by TTL for rerouting.

**"What happens on a cache miss?"**
> The OCA fetches from a parent OCA (IXP level) or origin (S3), serves the client immediately while caching simultaneously. Misses are rare (~5%) because of proactive push. Miss categories: new-content (before nightly fill), popularity (not pre-positioned), eviction (LRU removed it), or hardware failure.

**"Why doesn't YouTube use the same approach?"**
> YouTube has 800M+ videos with extreme long-tail distribution and 500 hours uploaded per minute. Proactive push is infeasible — you can't predict which of 800M videos will be requested and pre-position them all. Netflix's catalog of ~15K titles with concentrated, predictable viewership makes proactive push viable.

---

*Next: [06-adaptive-streaming.md](06-adaptive-streaming.md) — How Netflix dynamically adjusts video quality per-shot using per-title encoding and adaptive bitrate streaming.*
