# Deep Dive: Bot Detection & View Validation

## Table of Contents
1. [Why Fraud Detection Matters](#1-why-fraud-detection-matters)
2. [View Validation Pipeline (Multi-Stage)](#2-view-validation-pipeline-multi-stage)
3. [The "301 Views" Freeze](#3-the-301-views-freeze)
4. [Retroactive Count Adjustment](#4-retroactive-count-adjustment)
5. [Architecture Diagram](#5-architecture-diagram)
6. [ML Model Details](#6-ml-model-details)

---

## 1. Why Fraud Detection Matters

### The Financial Stakes

Every view on a monetized video translates directly to ad revenue. The chain is simple:

```
View → Ad Impression → CPM Revenue → Creator Payout
```

| Metric | Approximate Value |
|--------|-------------------|
| Average CPM (US) | $5–$12 |
| Views needed for $1,000 | ~100K–200K |
| YouTube's annual ad revenue (2024) | ~$31B |
| Estimated fraudulent view attempts/day | Billions |

If even 1% of fraudulent views slip through on that $31B base, that is $310M in misallocated ad spend per year. Advertisers lose trust, CPMs drop across the platform, and legitimate creators earn less.

### Beyond Money: Platform Integrity

View counts are not just a vanity metric. They are a **core input signal** to multiple systems:

- **Trending / Explore**: Inflated views push garbage content onto Trending, degrading user trust.
- **Recommendations**: The recommendation engine uses view velocity (views/hour) as a ranking signal. Fake views pollute collaborative filtering models.
- **Search Ranking**: Watch time and view count influence search result ordering.
- **Creator Monetization Tiers**: YouTube Partner Program requires 4,000 watch hours + 1,000 subscribers. Fraud can game eligibility.
- **Advertiser Confidence**: Brands pay based on verified impressions. If verification is weak, they move budget to competitors (TikTok, Meta).

### Scale of the Problem

YouTube has publicly stated that they reject approximately **15–20% of all incoming view events** as invalid. At YouTube's scale (~800M videos, ~1B hours watched/day), this means:

- Tens of billions of view events ingested daily
- Billions rejected as fraudulent
- Fraud detection must operate at sub-100ms latency for real-time stages
- Offline stages process petabytes of event data nightly

### Who Is Attacking?

| Attacker Type | Motivation | Sophistication |
|---------------|------------|----------------|
| Casual bots | Inflate own video | Low — simple scripts, curl loops |
| View farms | Sell views as a service ($5/10K views) | Medium — rotating proxies, browser automation |
| State-level manipulation | Political propaganda, disinformation | High — real devices, distributed globally |
| Competitor sabotage | Get rival's channel demonetized via suspicious traffic | Medium-High |
| Ad fraud rings | Generate CPM revenue on stolen/cloned content | High — sophisticated browser emulation |

The system must handle all of these simultaneously, with different detection strategies for each.

---

## 2. View Validation Pipeline (Multi-Stage)

The core design principle is **defense in depth**. No single check catches everything. Instead, views pass through progressively more expensive and more accurate validation stages.

```
                    Incoming View Event
                           │
                    ┌──────▼──────┐
                    │   Stage 1   │  Real-Time (~1ms)
                    │ Client-Side │  Client fingerprinting
                    │   Signals   │  Reject obvious bots
                    └──────┬──────┘
                           │ pass
                    ┌──────▼──────┐
                    │   Stage 2   │  Real-Time (~5ms)
                    │    Rate     │  Redis counters
                    │  Limiting   │  Reject floods
                    └──────┬──────┘
                           │ pass
                    ┌──────▼──────┐
                    │   Stage 3   │  Near Real-Time (~30s–5min)
                    │   Watch     │  Behavioral signals
                    │  Behavior   │  Reject shallow views
                    └──────┬──────┘
                           │ pass (tentatively counted)
                    ┌──────▼──────┐
                    │   Stage 4   │  Offline (hours–days)
                    │  Batch ML   │  Pattern analysis
                    │  Analysis   │  Retroactive subtraction
                    └─────────────┘
```

Each stage has a different **cost/accuracy tradeoff**:

| Stage | Latency | Cost per Event | Accuracy | False Positive Rate |
|-------|---------|----------------|----------|---------------------|
| 1 — Client Signals | <1ms | Negligible | Low (catches ~40% of bots) | Very low |
| 2 — Rate Limiting | ~5ms | Low (Redis lookup) | Medium (catches ~30% more) | Low |
| 3 — Watch Behavior | 30s–5min | Medium (state tracking) | High | Medium |
| 4 — Batch ML | Hours | High (Spark cluster) | Very high | Very low |

---

### Stage 1: Client-Side Signals (Real-Time)

**Goal**: Reject the cheapest, most obvious bots before they consume any backend resources.

This stage runs at the **edge layer** (CDN / API gateway) and examines the HTTP request itself.

#### 1a. User-Agent Validation

The simplest check. A surprising number of bot scripts use default or missing User-Agents.

```python
# Pseudocode at edge layer
KNOWN_BOT_UA_PATTERNS = [
    r"^python-requests",
    r"^curl/",
    r"^wget/",
    r"^Go-http-client",
    r"^Java/",          # default Java HttpURLConnection UA
    r"^$",              # empty UA
    r"Googlebot",       # search crawlers should not count as views
    r"Baiduspider",
]

def check_user_agent(request):
    ua = request.headers.get("User-Agent", "")
    for pattern in KNOWN_BOT_UA_PATTERNS:
        if re.match(pattern, ua):
            return REJECT
    # Also reject UAs that are exact matches to known browser strings
    # (real browsers have slight variations per OS/version)
    if ua in EXACT_KNOWN_BROWSER_STRINGS:
        return FLAG_SUSPICIOUS  # not reject, but increase suspicion score
    return PASS
```

**Limitations**: Trivially spoofed. Any attacker can set a realistic User-Agent. This catches only the laziest bots.

#### 1b. JavaScript Fingerprinting

The view event endpoint requires a **client-generated token** that proves JavaScript executed in a real browser environment. The token encodes:

- `navigator.webdriver` — true if Selenium/Puppeteer is controlling the browser
- Canvas fingerprint — rendering a hidden canvas element and hashing the pixel data
- WebGL renderer string — identifies GPU (headless browsers often have "SwiftShader")
- Screen resolution and color depth
- Installed plugins and fonts
- Timing-based checks — how long it took to execute certain JS operations

```javascript
// Simplified view token generation (client-side)
async function generateViewToken(videoId) {
    const signals = {
        webdriver: navigator.webdriver,                    // false in real browsers
        canvas: await getCanvasFingerprint(),
        webgl: getWebGLRenderer(),                         // "SwiftShader" = headless
        screen: `${screen.width}x${screen.height}x${screen.colorDepth}`,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        languages: navigator.languages,
        touchSupport: navigator.maxTouchPoints,
        deviceMemory: navigator.deviceMemory,
        hardwareConcurrency: navigator.hardwareConcurrency,
        timestamp: Date.now(),
    };

    // Sign with a rotating key embedded in the JS bundle
    // (obfuscated — not trivially extractable)
    const token = sign(signals, ROTATING_KEY);
    return token;
}

// The view event request includes this token
fetch("/api/v1/view", {
    method: "POST",
    body: JSON.stringify({ videoId, viewToken: token }),
});
```

**Key defense**: The JS bundle that generates this token is **obfuscated and rotated regularly** (e.g., weekly). Bot operators must reverse-engineer the new bundle each time.

#### 1c. TLS Fingerprinting (JA3)

Every TLS client hello has a unique fingerprint based on the cipher suites, extensions, and elliptic curves offered. This is called a **JA3 hash**.

```
Real Chrome on macOS:   JA3 = 771,4865-4866-4867-49195-49199-49196-...
Python requests:        JA3 = 771,49196-49200-159-52393-52392-...
Go net/http:            JA3 = 771,49195-49199-49196-49200-52393-...
```

The edge layer computes the JA3 hash from the raw TLS handshake and cross-references it against the claimed User-Agent:

```
IF User-Agent claims "Chrome/120" BUT JA3 matches "python-requests"
THEN REJECT — the client is lying about what it is
```

**Why this is powerful**: JA3 is computed from the TLS handshake, which happens before any HTTP headers are sent. It cannot be spoofed by simply setting headers. To fake a Chrome JA3, you must actually use Chrome's TLS stack (or a library like `curl-impersonate`).

#### 1d. Cookie and Session Validation

- First-party cookies prove the client has visited YouTube before and has a session.
- Absence of cookies on a "returning user" claim is suspicious.
- Cookie age and consistency are checked — a cookie minted 2 seconds ago claiming 500 prior visits is fraudulent.

#### 1e. reCAPTCHA / Challenge Integration

Not shown on every view (that would destroy UX), but triggered when suspicion score crosses a threshold:

```
IF suspicion_score > 0.7 AND user_not_logged_in:
    show_invisible_recaptcha()
    # or in extreme cases, show interactive challenge
```

YouTube uses **reCAPTCHA v3** (invisible, score-based) for most cases. The score (0.0 = likely bot, 1.0 = likely human) feeds into the overall suspicion model.

---

### Stage 2: Rate Limiting (Real-Time)

**Goal**: Prevent any single source from generating an unreasonable number of views, even if each individual request passes Stage 1.

Rate limiting operates on multiple dimensions simultaneously using **sliding window counters in Redis**.

#### Dimensions

| Dimension | Window | Limit | Rationale |
|-----------|--------|-------|-----------|
| (IP, videoId) | 1 hour | 1 view | Same IP watching same video repeatedly is suspicious |
| (IP, *) | 1 hour | 50 views | One IP watching 50 different videos/hour — likely a bot |
| (userId, videoId) | 24 hours | 3 views | Logged-in user rewatching — allow some replays |
| (userId, *) | 1 hour | 30 views | Logged-in user binge-watching — generous but bounded |
| (IP /24 subnet, videoId) | 1 hour | 10 views | Entire subnet targeting one video — coordinated attack |
| (deviceFingerprint, videoId) | 24 hours | 1 view | Same device, even across IPs (VPN rotation) |

#### Redis Implementation: Sliding Window Counter

A fixed window counter has an edge problem: 99 requests at 11:59 + 99 at 12:01 = 198 in 2 minutes, while the limit is 100/hour. The **sliding window log** approach fixes this but uses more memory. The practical middle ground is a **sliding window counter**:

```python
def is_rate_limited(key: str, window_seconds: int, max_count: int) -> bool:
    """
    Sliding window counter using two fixed windows.
    Key idea: interpolate between current and previous window.
    """
    now = time.time()
    current_window = int(now // window_seconds)
    previous_window = current_window - 1

    current_key = f"{key}:{current_window}"
    previous_key = f"{key}:{previous_window}"

    # MGET both counters in one round trip
    current_count, previous_count = redis.mget(current_key, previous_key)
    current_count = int(current_count or 0)
    previous_count = int(previous_count or 0)

    # How far into the current window are we? (0.0 to 1.0)
    elapsed_ratio = (now % window_seconds) / window_seconds

    # Weighted estimate of requests in the sliding window
    estimated_count = previous_count * (1 - elapsed_ratio) + current_count

    if estimated_count >= max_count:
        return True  # RATE LIMITED

    # Increment current window counter
    pipe = redis.pipeline()
    pipe.incr(current_key)
    pipe.expire(current_key, window_seconds * 2)  # TTL = 2 windows
    pipe.execute()

    return False
```

#### Composite Rate Limit Check

```python
def check_rate_limits(ip: str, user_id: str, video_id: str, device_fp: str) -> bool:
    checks = [
        (f"rl:ip:vid:{ip}:{video_id}",       3600,  1),
        (f"rl:ip:all:{ip}",                   3600,  50),
        (f"rl:dev:vid:{device_fp}:{video_id}", 86400, 1),
    ]
    if user_id:
        checks.append((f"rl:uid:vid:{user_id}:{video_id}", 86400, 3))
        checks.append((f"rl:uid:all:{user_id}",            3600,  30))

    for key, window, limit in checks:
        if is_rate_limited(key, window, limit):
            return True  # REJECTED
    return False
```

#### Handling Shared IPs (NAT, Universities, Corporate)

A major challenge: thousands of legitimate users behind a single IP (university campus, corporate NAT, mobile carrier-grade NAT). Aggressive per-IP limits cause false positives.

Mitigations:
- **IP reputation database**: Classify IPs as residential, datacenter, VPN, mobile carrier, university. Apply different limits per class.
- **Carrier-grade NAT ranges**: Known CGNAT ranges (e.g., 100.64.0.0/10) get much higher per-IP limits.
- **Logged-in user override**: If the user is logged in with a healthy account, per-IP limits are relaxed (per-user limits still apply).
- **Device fingerprint as tiebreaker**: Even behind the same IP, distinct device fingerprints get separate allowances.

```
IF ip_class == "datacenter" AND user_not_logged_in:
    limit = 5 views/hour/IP  (very strict)
ELIF ip_class == "residential":
    limit = 50 views/hour/IP  (normal)
ELIF ip_class == "university" OR ip_class == "cgnat":
    limit = 500 views/hour/IP  (relaxed, rely on device FP)
```

---

### Stage 3: Watch Behavior Analysis (Near Real-Time)

**Goal**: Verify that the user actually *watched* the video, not just loaded the page and immediately left.

This is where YouTube's view definition becomes critical: **a view only counts if the user watches for at least ~30 seconds (or the full video if shorter than 30s)**. Some sources cite a threshold of approximately 50% for short videos.

#### 3a. Minimum Watch Duration

The client sends periodic **heartbeat pings** during playback:

```javascript
// Client-side heartbeat (simplified)
let watchedSeconds = 0;
const HEARTBEAT_INTERVAL = 10; // seconds

video.addEventListener("timeupdate", () => {
    watchedSeconds = video.currentTime;
});

setInterval(() => {
    fetch("/api/v1/heartbeat", {
        method: "POST",
        body: JSON.stringify({
            videoId,
            sessionId,
            watchedSeconds,
            paused: video.paused,
            visible: !document.hidden,
            volume: video.volume,
            playbackRate: video.playbackRate,
        }),
    });
}, HEARTBEAT_INTERVAL * 1000);
```

On the server side:

```python
def evaluate_watch_session(session: WatchSession) -> ViewValidity:
    video_duration = get_video_duration(session.video_id)
    threshold = min(30, video_duration * 0.5)

    if session.watched_seconds < threshold:
        return INVALID  # did not watch enough

    if session.heartbeat_count < (threshold / HEARTBEAT_INTERVAL) - 1:
        return INVALID  # missing heartbeats — tab was closed or faked

    return TENTATIVELY_VALID
```

#### 3b. Page Visibility API

The browser's `document.hidden` property and `visibilitychange` event tell us if the tab is in the foreground. Watch time accumulated while the tab is hidden does not count.

```javascript
document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
        // Pause watch time accumulation
        sendEvent("tab_hidden");
    } else {
        sendEvent("tab_visible");
    }
});
```

**Why this matters**: A common bot technique is to open hundreds of tabs simultaneously. Only the foreground tab has `document.hidden === false`. The others accumulate fake watch time that the server discounts.

#### 3c. User Interaction Signals

The server also considers whether the user interacted with the page at all:

| Signal | Weight | Notes |
|--------|--------|-------|
| Mouse movement | Low | Easy to fake but adds to composite score |
| Scroll events | Low | Same |
| Click on player controls | Medium | Play/pause, seek, volume |
| Comment, like, share | High | Strong indicator of real engagement |
| Seek behavior | Medium | Real users seek; bots typically don't |
| Video quality change | Low | Auto or manual quality switches |

These signals are not individually decisive but feed into a **composite engagement score** that Stage 4 uses for ML features.

#### 3d. Server-Side Session State Machine

Each watch session follows a state machine on the server:

```
                    ┌─────────────┐
       view event   │   STARTED   │
       received ──► │  (timer=0)  │
                    └──────┬──────┘
                           │ heartbeats arriving
                    ┌──────▼──────┐
                    │  WATCHING   │
                    │ (timer > 0) │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     timer < threshold   timer ≥ threshold   no heartbeat
     AND tab hidden      AND tab visible     for 60s
              │            │            │
       ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
       │  ABANDONED  │  │  COUNTED    │  │   TIMED_OUT │
       │  (invalid)  │  │  (valid*)   │  │  (invalid)  │
       └─────────────┘  └─────────────┘  └─────────────┘

       * = tentatively valid, subject to Stage 4 review
```

Session state is stored in Redis with a TTL of 10 minutes (sessions older than that are abandoned):

```
Key:    session:{sessionId}
Value:  {videoId, userId, ip, startTime, watchedSec, heartbeats, state}
TTL:    600 seconds
```

---

### Stage 4: Batch Fraud Analysis (Offline)

**Goal**: Detect sophisticated fraud that is invisible at the individual-event level but obvious in aggregate patterns.

This is where the real heavy lifting happens. Stages 1–3 catch the low-hanging fruit. Stage 4 catches **coordinated, distributed attacks** using ML models running on Spark/Flink over the full event log.

#### 4a. Input Data

The batch pipeline ingests the full event stream from Kafka into a data lake (HDFS / object storage):

```
view_events/
├── date=2026-02-27/
│   ├── hour=00/
│   │   ├── part-00000.parquet
│   │   ├── part-00001.parquet
│   │   └── ...
│   ├── hour=01/
│   └── ...
```

Each event record includes:

```
{
  "event_id": "uuid",
  "video_id": "dQw4w9WgXcQ",
  "user_id": "U123" | null,
  "ip": "203.0.113.42",
  "device_fingerprint": "fp_abc123",
  "user_agent": "Mozilla/5.0 ...",
  "ja3_hash": "e7d705a3286e19ea42f587b344ee6865",
  "country": "US",
  "region": "CA",
  "watched_seconds": 47,
  "video_duration": 312,
  "heartbeat_count": 5,
  "engagement_score": 0.3,
  "timestamp": "2026-02-27T14:23:01Z",
  "stage1_score": 0.1,
  "stage2_passed": true,
  "stage3_state": "COUNTED"
}
```

#### 4b. Pattern Detection (Spark Jobs)

**Geographic Anomalies**

A video by an English-speaking creator in the US suddenly gets 500K views from a specific city in a country with no prior audience:

```sql
-- Spark SQL: detect geographic anomalies
WITH video_geo AS (
    SELECT
        video_id,
        country,
        COUNT(*) as view_count,
        COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY video_id) as pct
    FROM view_events
    WHERE date = '2026-02-27'
    GROUP BY video_id, country
),
historical_geo AS (
    SELECT video_id, country, avg_pct
    FROM video_country_baseline  -- rolling 30-day average
)
SELECT
    v.video_id, v.country, v.view_count, v.pct,
    h.avg_pct,
    v.pct - COALESCE(h.avg_pct, 0) as geo_anomaly_score
FROM video_geo v
LEFT JOIN historical_geo h ON v.video_id = h.video_id AND v.country = h.country
WHERE v.pct - COALESCE(h.avg_pct, 0) > 20  -- 20%+ deviation
ORDER BY geo_anomaly_score DESC;
```

**Temporal Anomalies**

Organic view velocity follows a natural curve: spike at upload, exponential decay. Bot traffic creates unnatural patterns:

```
Organic:    ████████░░░░░░░░░░░░░░░░░░
Bot spike:  ░░░░░░░░░░████████████░░░░  (sudden burst hours later)
Bot steady: ████████████████████████████  (unnaturally flat)
```

**Device Fingerprint Clustering**

If 10,000 "different" devices share the same canvas fingerprint, WebGL renderer, screen resolution, and language settings, they are likely the same bot farm:

```python
# PySpark: cluster suspiciously similar devices
from pyspark.ml.feature import VectorAssembler
from pyspark.ml.clustering import DBSCAN  # conceptual; use custom impl

features = [
    "canvas_hash", "webgl_hash", "screen_hash",
    "language_hash", "timezone_hash", "plugin_hash"
]

# Hash all categorical features to numeric
df = hash_features(view_events_df, features)

assembler = VectorAssembler(inputCols=features, outputCol="feature_vector")
df = assembler.transform(df)

# Find clusters of near-identical fingerprints
# Clusters > 100 devices with identical features are suspicious
clusters = df.groupBy(features).count().filter("count > 100")

# Flag all views from devices in suspicious clusters
flagged_devices = clusters.select("device_fingerprint")
```

**User Account Age / Behavior Correlation**

Accounts created in bulk specifically for view fraud share patterns:
- Created within hours of each other
- No profile picture, no subscriptions, no playlists
- Only activity is watching a small set of videos
- Similar usernames (auto-generated patterns)

#### 4c. Coordination Detection

The most sophisticated attack uses **real devices and real humans** (click farms). Detection shifts from technical signals to behavioral patterns:

```
Indicator                          | Weight
-----------------------------------|--------
Multiple accounts created same day | High
All watching same video within     |
  a narrow time window             | High
No other activity on accounts      | High
Views from same /24 subnet         | Medium
Sequential watch start times       | High
  (1s apart — scripted)            |
Identical watch duration           | Medium
  (all stop at exactly 31s)        |
```

---

## 3. The "301 Views" Freeze

### Historical Context

From approximately 2012 to 2015, YouTube employed a blunt mechanism for fraud prevention: when a video's view count reached **301** (originally 300+1), the counter would freeze and display "301+" while the system performed additional verification.

```
Upload → Views grow → Hits 301 → Counter freezes → "301+ views"
                                                         │
                            (hours to days later)        │
                                                         ▼
                                              Counter unfreezes
                                              with verified count
```

### Why 301?

The number was not arbitrary. YouTube's original system processed views in two stages:

1. **Fast counter**: Incremented immediately, visible on the page. No validation.
2. **Slow verification**: Batch job that validated each view against fraud rules.

The fast counter was allowed to run unverified up to ~300. Beyond that threshold, the risk of displaying a fraudulently inflated count became material (a video with "1M views" that drops to "50K" is worse UX than freezing at 301). So the counter paused at 301 until the slow verification caught up.

### Why It Was Removed (~2015)

Several factors led to its removal:
- **Improved real-time validation**: Stages 1–3 became fast and accurate enough to verify most views in real time.
- **User confusion**: "Why is every video stuck at 301?" became a meme and a source of genuine creator frustration.
- **Competitive pressure**: Other platforms (Facebook, Instagram) showed counts immediately. The freeze made YouTube look technically inferior.
- **Better retroactive correction**: YouTube became confident in its ability to add counts quickly and subtract them later if fraud was detected.

The replacement approach: **count optimistically in near-real-time, correct retroactively in batch**. This is the current architecture.

---

## 4. Retroactive Count Adjustment

### Counts Can Go Down

This is a critical design property. YouTube's public view counter is **not monotonically increasing**. After Stage 4 batch analysis flags views as fraudulent, those counts are subtracted.

```
Day 1:  Video shows 1,000,000 views
Day 2:  Batch job detects 200,000 fraudulent views
Day 3:  Video shows 800,000 views
```

This has caused public controversy (creators seeing counts drop), but it is essential for advertiser trust.

### Implementation: Atomic Adjustment with Audit Trail

```python
# Pseudocode: retroactive count adjustment
def apply_fraud_adjustment(video_id: str, fraudulent_count: int, batch_job_id: str):
    """
    Atomically subtract fraudulent views and record an audit entry.
    Uses a transaction to ensure consistency.
    """
    with database.transaction():
        # 1. Read current count
        current = db.execute(
            "SELECT view_count FROM video_stats WHERE video_id = %s FOR UPDATE",
            (video_id,)
        )

        # 2. Compute adjusted count (never go below 0)
        adjusted = max(0, current.view_count - fraudulent_count)

        # 3. Update the count
        db.execute(
            "UPDATE video_stats SET view_count = %s WHERE video_id = %s",
            (adjusted, video_id)
        )

        # 4. Write audit record
        db.execute(
            """INSERT INTO view_adjustments
               (video_id, previous_count, new_count, delta, reason, batch_job_id, timestamp)
               VALUES (%s, %s, %s, %s, 'fraud_detection', %s, NOW())""",
            (video_id, current.view_count, adjusted, -fraudulent_count, batch_job_id)
        )

        # 5. Recalculate derived metrics
        recalculate_revenue(video_id, adjusted)
        invalidate_cache(video_id)
```

### Audit Trail Schema

```sql
CREATE TABLE view_adjustments (
    id              BIGSERIAL PRIMARY KEY,
    video_id        VARCHAR(20) NOT NULL,
    previous_count  BIGINT NOT NULL,
    new_count       BIGINT NOT NULL,
    delta           BIGINT NOT NULL,          -- negative for subtractions
    reason          VARCHAR(50) NOT NULL,      -- 'fraud_detection', 'manual_review', 'spam_purge'
    batch_job_id    VARCHAR(64),               -- links to the Spark job that found the fraud
    reviewed_by     VARCHAR(64),               -- null for automated, employee ID for manual
    timestamp       TIMESTAMP NOT NULL,
    INDEX idx_video_ts (video_id, timestamp)
);
```

### Revenue Clawback

When views are subtracted, the corresponding ad revenue must also be adjusted:

```
Fraudulent views removed:  200,000
Estimated CPM:             $8
Revenue to claw back:      200,000 / 1000 * $8 = $1,600
```

This clawback affects the **creator's next payout cycle**, not retroactively (YouTube absorbs the loss for already-paid amounts, then adjusts future payments).

---

## 5. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         COMPLETE FRAUD DETECTION PIPELINE                   │
└─────────────────────────────────────────────────────────────────────────────┘

  Client (Browser/App)
  ┌──────────────────────────────────────────┐
  │  JS Fingerprint Engine                   │
  │  ┌────────────┐  ┌────────────────────┐  │
  │  │ Heartbeats │  │ View Token (signed) │  │
  │  │ every 10s  │  │ canvas + webgl +   │  │
  │  └─────┬──────┘  │ webdriver + ...     │  │
  │        │         └─────────┬──────────┘  │
  └────────┼───────────────────┼─────────────┘
           │                   │
           ▼                   ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  EDGE / CDN LAYER                                                  │
  │  ┌──────────────────┐  ┌─────────────────┐  ┌──────────────────┐  │
  │  │ TLS Termination  │  │ JA3 Fingerprint │  │ IP Reputation    │  │
  │  │ (extract JA3)    │─►│ Check           │─►│ Lookup           │  │
  │  └──────────────────┘  └─────────────────┘  └────────┬─────────┘  │
  │                                          STAGE 1     │            │
  │  ┌──────────────────┐  ┌─────────────────┐           │            │
  │  │ UA Validation    │  │ View Token      │◄──────────┘            │
  │  │                  │─►│ Verification    │                        │
  │  └──────────────────┘  └────────┬────────┘                        │
  └─────────────────────────────────┼─────────────────────────────────┘
                                    │ pass
                                    ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  API GATEWAY / VIEW SERVICE                                        │
  │                                                                    │
  │  ┌─────────────────────────────────────┐                           │
  │  │         STAGE 2: Rate Limiter       │                           │
  │  │  ┌───────────────────────────────┐  │                           │
  │  │  │       Redis Cluster           │  │                           │
  │  │  │  ┌─────────┐  ┌───────────┐  │  │                           │
  │  │  │  │ IP:vid  │  │ user:vid  │  │  │                           │
  │  │  │  │ counter │  │ counter   │  │  │                           │
  │  │  │  └─────────┘  └───────────┘  │  │                           │
  │  │  │  ┌─────────┐  ┌───────────┐  │  │                           │
  │  │  │  │ IP:all  │  │ device:vid│  │  │                           │
  │  │  │  │ counter │  │ counter   │  │  │                           │
  │  │  │  └─────────┘  └───────────┘  │  │                           │
  │  │  └───────────────────────────────┘  │                           │
  │  └──────────────────┬──────────────────┘                           │
  │                     │ pass                                         │
  │                     ▼                                              │
  │  ┌──────────────────────────────────┐     ┌─────────────────────┐  │
  │  │  Session Manager (Stage 3)      │     │ View Counter        │  │
  │  │  ┌────────────────────────────┐ │     │ (increment on       │  │
  │  │  │ Redis: session state       │ │────►│  COUNTED state)     │  │
  │  │  │ {watchedSec, heartbeats,   │ │     └──────────┬──────────┘  │
  │  │  │  visibility, interactions} │ │                │             │
  │  │  └────────────────────────────┘ │                │             │
  │  └─────────────────────────────────┘                │             │
  └─────────────────────────────────────────────────────┼─────────────┘
                                                        │
           ┌────────────────────────────────────────────┘
           │
           ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  EVENT STREAMING (Kafka)                                           │
  │                                                                    │
  │  Topic: view-events           Topic: view-verdicts                 │
  │  ┌─────┬─────┬─────┬────┐    ┌─────┬─────┬─────┬────┐            │
  │  │  P0 │  P1 │  P2 │ .. │    │  P0 │  P1 │  P2 │ .. │            │
  │  └──┬──┴──┬──┴──┬──┴────┘    └─────┴─────┴─────┴────┘            │
  │     │     │     │                     ▲                            │
  └─────┼─────┼─────┼─────────────────────┼────────────────────────────┘
        │     │     │                     │
        ▼     ▼     ▼                     │
  ┌────────────────────────────────────────────────────────────────────┐
  │  BATCH PROCESSING (Stage 4)                                        │
  │                                                                    │
  │  ┌──────────────────────┐    ┌──────────────────────────────────┐  │
  │  │  Data Lake (HDFS/S3) │    │  Spark Cluster                  │  │
  │  │  ┌────────────────┐  │    │  ┌────────────────────────────┐ │  │
  │  │  │ view_events/   │  │───►│  │ Geographic Anomaly Job    │ │  │
  │  │  │  date=.../     │  │    │  ├────────────────────────────┤ │  │
  │  │  │  hour=.../     │  │    │  │ Temporal Pattern Job      │ │  │
  │  │  └────────────────┘  │    │  ├────────────────────────────┤ │  │
  │  │  ┌────────────────┐  │    │  │ Device Clustering Job     │ │  │
  │  │  │ fraud_labels/  │  │◄───│  ├────────────────────────────┤ │  │
  │  │  │ (training data)│  │    │  │ Account Behavior Job      │ │  │
  │  │  └────────────────┘  │    │  ├────────────────────────────┤ │──┘
  │  └──────────────────────┘    │  │ ML Scoring Pipeline       │ │
  │                              │  └────────────────────────────┘ │
  │                              └──────────────┬─────────────────┘
  │                                             │
  └─────────────────────────────────────────────┼──────────────────────┘
                                                │
                                                ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  ADJUSTMENT SERVICE                                                │
  │  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
  │  │ Read fraud       │  │ Atomic count     │  │ Revenue          │  │
  │  │ verdicts         │─►│ subtraction      │─►│ clawback         │  │
  │  │                  │  │ + audit trail    │  │ calculation      │  │
  │  └─────────────────┘  └──────────────────┘  └──────────────────┘  │
  └────────────────────────────────────────────────────────────────────┘
```

---

## 6. ML Model Details

### Feature Engineering

The ML model consumes features at multiple granularities:

#### Per-View Features (extracted from each event)

| Feature | Type | Description |
|---------|------|-------------|
| `watched_ratio` | Float | watched_seconds / video_duration |
| `heartbeat_regularity` | Float | std_dev of heartbeat intervals (bots are too regular) |
| `engagement_score` | Float | Composite of interactions (clicks, seeks, etc.) |
| `ja3_ua_mismatch` | Bool | TLS fingerprint inconsistent with User-Agent |
| `ip_is_datacenter` | Bool | IP belongs to known cloud/hosting provider |
| `ip_is_vpn` | Bool | IP belongs to known VPN/proxy service |
| `account_age_days` | Int | Age of the user account (0 if anonymous) |
| `time_since_upload_hours` | Float | How recently was the video uploaded |
| `device_fp_uniqueness` | Float | How many other views share this fingerprint |
| `cookie_age_seconds` | Int | Age of the session cookie |
| `recaptcha_score` | Float | reCAPTCHA v3 score (0.0–1.0), null if not triggered |
| `playback_rate` | Float | Video playback speed (2x = suspicious for watch time farming) |

#### Per-Video Aggregate Features (computed over time windows)

| Feature | Type | Description |
|---------|------|-------------|
| `view_velocity_1h` | Int | Views in the last hour |
| `view_velocity_24h` | Int | Views in the last 24 hours |
| `velocity_ratio` | Float | 1h velocity / 24h velocity (spikes → high ratio) |
| `geo_entropy` | Float | Shannon entropy of country distribution (low = suspicious) |
| `unique_ip_ratio` | Float | unique IPs / total views (low = bot farm) |
| `unique_device_ratio` | Float | unique fingerprints / total views |
| `avg_watch_ratio` | Float | Mean watched_ratio across recent views |
| `watch_ratio_stddev` | Float | Std dev of watch ratios (bots are uniform) |
| `anonymous_view_pct` | Float | % of views from non-logged-in users |
| `new_account_pct` | Float | % of views from accounts < 7 days old |

#### Per-IP Aggregate Features

| Feature | Type | Description |
|---------|------|-------------|
| `videos_watched_1h` | Int | Distinct videos from this IP in 1 hour |
| `total_views_1h` | Int | Total view events from this IP |
| `user_diversity` | Float | Unique userIds per IP (1 = likely single user) |
| `avg_session_duration` | Float | Mean watch duration from this IP |

### Training Data

The model is trained on **labeled data** from multiple sources:

1. **Human review**: A team of content moderators manually labels samples of views as legitimate or fraudulent. This is the gold standard but expensive (~10K labels/week).
2. **Honeypot videos**: Unlisted videos with no organic way to discover them. Any views are definitionally fraudulent.
3. **Known bot purchases**: YouTube's trust & safety team periodically purchases views from bot services and labels the resulting traffic.
4. **High-confidence rule-based labels**: Views rejected by Stages 1–2 with very high confidence (e.g., `navigator.webdriver === true`) are auto-labeled as fraudulent.
5. **Creator reports**: When creators report suspicious activity on competitor videos, investigators label the traffic.

```
Training Data Composition (approximate):
┌─────────────────────────┬────────────┬──────────────┐
│ Source                  │ Volume     │ Label Quality│
├─────────────────────────┼────────────┼──────────────┤
│ Human review            │ ~10K/week  │ Very High    │
│ Honeypots               │ ~100K/week │ High         │
│ Purchased bot views     │ ~50K/month │ High         │
│ Rule-based auto-labels  │ ~10M/week  │ Medium       │
│ Creator reports         │ ~1K/week   │ Variable     │
└─────────────────────────┴────────────┴──────────────┘
```

### Model Architecture

The fraud detection system uses an **ensemble** approach:

```
                    Input Features
                         │
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
    ┌────────────┐ ┌──────────┐ ┌──────────────┐
    │ Gradient   │ │ Neural   │ │ Rule-Based   │
    │ Boosted    │ │ Network  │ │ Heuristics   │
    │ Trees      │ │ (DNN)    │ │              │
    │ (XGBoost)  │ │          │ │              │
    └─────┬──────┘ └────┬─────┘ └──────┬───────┘
          │              │              │
          ▼              ▼              ▼
    ┌─────────────────────────────────────────┐
    │         Ensemble Combiner               │
    │   weighted_score = 0.5*xgb + 0.3*dnn   │
    │                    + 0.2*rules          │
    │                                         │
    │   IF weighted_score > 0.8: FRAUDULENT   │
    │   IF weighted_score > 0.5: SUSPICIOUS   │
    │   ELSE: LEGITIMATE                      │
    └─────────────────────────────────────────┘
```

**Why this ensemble?**

| Model | Strengths | Weaknesses |
|-------|-----------|------------|
| XGBoost (GBT) | Handles tabular features well, interpretable feature importances, fast inference | Poor at sequential/temporal patterns |
| DNN | Can learn complex non-linear interactions, handles embeddings for categorical features | Slower inference, harder to interpret |
| Rule-based | Deterministic, explainable, zero false positives on known patterns | Cannot generalize to new attack types |

The rule-based component acts as a **safety net** — it catches known fraud patterns with 100% precision, while the ML models handle novel attacks.

### Model Training Pipeline

```
┌──────────┐    ┌─────────────┐    ┌──────────────┐    ┌────────────┐
│ Raw view │    │ Feature     │    │ Train/Val    │    │ Model      │
│ events + │───►│ extraction  │───►│ split        │───►│ training   │
│ labels   │    │ (Spark)     │    │ (time-based) │    │ (XGBoost + │
└──────────┘    └─────────────┘    └──────────────┘    │  DNN)      │
                                                       └─────┬──────┘
                                                             │
                ┌─────────────┐    ┌──────────────┐          │
                │ Deploy to   │◄───│ A/B test     │◄─────────┘
                │ production  │    │ evaluation   │
                └─────────────┘    └──────────────┘
```

**Key training details**:

- **Time-based split**: Never train on future data. Train on weeks 1–4, validate on week 5, test on week 6.
- **Retraining cadence**: Weekly. Bot operators adapt, so the model must also adapt.
- **Class imbalance**: ~80% of views are legitimate. Use SMOTE oversampling or class weights.
- **Evaluation metrics**: Primarily **precision at high recall** — we want to catch most bots (high recall) without falsely flagging legitimate views (high precision). Target: 95% recall at 99% precision.

### Online Learning / Model Updates

The weekly retraining cycle has a gap: a new attack pattern on Monday is not caught until the next model deploys on Sunday. To close this gap:

1. **Feature store with real-time aggregates**: Flink streaming jobs continuously update per-IP, per-video, and per-device aggregate features. Even without retraining the model, the input features change in real time.

2. **Threshold tuning**: The fraud score threshold (e.g., 0.8) can be adjusted without retraining. If a new attack is detected manually, operators can lower the threshold temporarily.

3. **Rule injection**: New heuristic rules can be deployed within hours (no ML retraining needed). Example:
   ```python
   # Emergency rule: block views from a specific bot network
   # Deployed via config, no code push needed
   {
       "rule_id": "emergency_2026_02_27_001",
       "condition": "ja3_hash IN ('abc123', 'def456') AND ip_subnet = '198.51.100.0/24'",
       "action": "REJECT",
       "expires": "2026-03-06T00:00:00Z"
   }
   ```

4. **Incremental / online learning** (experimental): Some teams explore updating model weights on streaming data using frameworks like River (Python) or Vowpal Wabbit, but this is not yet mainstream for high-stakes fraud detection due to the risk of **adversarial poisoning** — attackers could intentionally send patterns designed to shift the model's decision boundary.

### Model Monitoring

A deployed fraud model must be continuously monitored for:

| Metric | Alert Threshold | Meaning |
|--------|-----------------|---------|
| Fraud rate (% rejected) | < 10% or > 30% | Model is too lenient or too aggressive |
| False positive rate | > 1% (sampled) | Legitimate views being rejected |
| Feature drift | KL divergence > 0.1 | Input distribution has shifted — retrain needed |
| Prediction latency (p99) | > 50ms | Model too slow for near-real-time scoring |
| Label agreement rate | < 90% | Model disagrees with human reviewers too often |

```python
# Monitoring pseudocode (runs every hour)
def monitor_fraud_model():
    recent_predictions = get_predictions(last_hour=True)

    fraud_rate = recent_predictions.filter(score > 0.8).count() / recent_predictions.count()
    if fraud_rate < 0.10 or fraud_rate > 0.30:
        alert("FRAUD_RATE_ANOMALY", fraud_rate)

    # Sample 1000 predictions, compare with human labels
    sample = recent_predictions.sample(1000)
    human_labels = get_human_review(sample)
    agreement = compute_agreement(sample.predictions, human_labels)
    if agreement < 0.90:
        alert("MODEL_ACCURACY_DEGRADATION", agreement)
```

---

## Summary

The fraud detection system is not a single check but a **layered pipeline** where each stage adds cost but also accuracy. The real-time stages (1–3) provide immediate protection at low cost, while the batch stage (4) provides deep analysis that catches sophisticated attacks. The key engineering insight is that **optimistic counting with retroactive correction** gives better UX than pessimistic counting (the old 301 freeze), as long as the retroactive correction is reliable and auditable.

| Property | Value |
|----------|-------|
| Real-time rejection rate | ~15–20% of all view events |
| Real-time latency budget | < 50ms for Stages 1–2 |
| Batch analysis window | 6–24 hours after view event |
| ML model retraining | Weekly |
| False positive target | < 1% |
| Audit trail retention | Indefinite (compliance requirement) |
