# View Counter System — Comprehensive API Reference

> This document is the exhaustive API reference for a YouTube-like View Counter system.
> The [interview simulation](01-interview-simulation.md) covers a subset of these APIs; endpoints discussed there are marked with a star (**\***).

---

## 1. View Ingestion APIs

The core write path. **Highest traffic surface** — every video play triggers a view event. Must handle millions of events/sec for viral videos.

### `POST /v1/videos/{videoId}/view` **\***

Record a view event. Fire-and-forget from the client's perspective — the API returns `202 Accepted` immediately, and the event is processed asynchronously via Kafka.

**Request:**
```json
POST /v1/videos/abc123/view
Content-Type: application/json
X-Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000

{
  "userId": "user_789",           // null for anonymous
  "sessionId": "sess_456",        // always present (anonymous or logged-in)
  "timestamp": 1709078400000,     // client-side event time (epoch ms)
  "deviceType": "mobile",         // mobile | desktop | tablet | tv | embedded
  "country": "US",                // ISO 3166-1 alpha-2 (derived from IP if absent)
  "referrer": "search",           // search | suggested | external | direct | notification
  "watchDurationSeconds": 47,     // how long the user watched
  "videoDurationSeconds": 240,    // total video length
  "clientFingerprint": "fp_abc",  // browser/device fingerprint for fraud detection
  "playerState": "foreground"     // foreground | background | pip
}
```

**Response:**
```json
HTTP/1.1 202 Accepted
{
  "status": "accepted",
  "eventId": "evt_20240228_abc123_550e8400"
}
```

**Notes:**
- `202 Accepted` — the view is queued, not yet counted. Processing happens asynchronously.
- `X-Idempotency-Key` prevents duplicate counting on network retries.
- Minimum `watchDurationSeconds` validation: must be ≥ 30s or ≥ 50% of `videoDurationSeconds`, whichever is shorter, for the view to count.
- Rate limited per (IP, videoId) and per (userId, videoId).

---

### `POST /v1/videos/{videoId}/view/heartbeat`

Periodic heartbeat during video playback. Sent every 10-30 seconds. Used for:
- Updating `watchDurationSeconds` continuously
- Detecting tab visibility changes
- Fraud signal: bots rarely send realistic heartbeat patterns

**Request:**
```json
POST /v1/videos/abc123/view/heartbeat
{
  "sessionId": "sess_456",
  "eventId": "evt_20240228_abc123_550e8400",
  "currentWatchSeconds": 87,
  "isVisible": true,
  "hasInteraction": true
}
```

**Response:**
```json
HTTP/1.1 204 No Content
```

---

## 2. View Count Read APIs

The read path. Must be extremely fast — these are called on every page render.

### `GET /v1/videos/{videoId}/viewCount` **\***

Return the current view count for a single video. This is what appears below every YouTube video.

**Request:**
```
GET /v1/videos/abc123/viewCount
```

**Response:**
```json
HTTP/1.1 200 OK
Cache-Control: public, max-age=30
{
  "videoId": "abc123",
  "viewCount": 1284739201,
  "formattedCount": "1.2B views",
  "lastUpdated": "2024-02-28T10:30:00Z",
  "isFrozen": false
}
```

**Notes:**
- Target latency: < 10ms (p99).
- CDN-cacheable with 30s TTL. Stale counts are acceptable.
- `isFrozen: true` when view count is under fraud investigation.
- `formattedCount` saves client-side formatting logic.

---

### `GET /v1/videos/batch/viewCounts` **\***

Batch fetch view counts for multiple videos. Used on homepage, search results, recommendation feeds.

**Request:**
```
GET /v1/videos/batch/viewCounts?ids=abc123,def456,ghi789,...
```

**Response:**
```json
HTTP/1.1 200 OK
Cache-Control: public, max-age=30
{
  "counts": [
    { "videoId": "abc123", "viewCount": 1284739201, "formattedCount": "1.2B views" },
    { "videoId": "def456", "viewCount": 58291, "formattedCount": "58K views" },
    { "videoId": "ghi789", "viewCount": 4102837, "formattedCount": "4.1M views" }
  ],
  "missing": []
}
```

**Notes:**
- Max 100 video IDs per request.
- Target latency: < 50ms (p99) for a batch of 50.
- Internally uses Redis `MGET` for single-roundtrip fetch.
- `missing` array contains IDs not found (deleted or never-existed videos).

---

### `GET /v1/channels/{channelId}/totalViews`

Aggregate view count across all videos for a channel. Used in YouTube Studio dashboard and channel pages.

**Request:**
```
GET /v1/channels/ch_001/totalViews
```

**Response:**
```json
HTTP/1.1 200 OK
{
  "channelId": "ch_001",
  "totalViews": 98234719283,
  "formattedCount": "98.2B views",
  "videoCount": 1247,
  "lastUpdated": "2024-02-28T10:00:00Z"
}
```

**Notes:**
- Pre-computed aggregate, updated every 1-5 minutes. Not computed on-the-fly.
- Stored as a separate counter, incremented by the aggregation pipeline.

---

## 3. Analytics / Breakdown APIs

Time-series and demographic view data. Used in YouTube Studio (creator dashboard). Higher latency acceptable (1-2s).

### `GET /v1/videos/{videoId}/analytics` **\***

Time-series view data for a video. Views per hour/day/week for a given date range.

**Request:**
```
GET /v1/videos/abc123/analytics?startDate=2024-02-01&endDate=2024-02-28&granularity=day
```

**Query Parameters:**
| Parameter | Type | Required | Values | Default |
|-----------|------|----------|--------|---------|
| `startDate` | date | yes | ISO 8601 date | — |
| `endDate` | date | yes | ISO 8601 date | — |
| `granularity` | enum | no | `minute`, `hour`, `day`, `week`, `month` | `day` |
| `timezone` | string | no | IANA timezone | `UTC` |

**Response:**
```json
HTTP/1.1 200 OK
{
  "videoId": "abc123",
  "granularity": "day",
  "timezone": "UTC",
  "dataPoints": [
    { "timestamp": "2024-02-01T00:00:00Z", "views": 142391, "uniqueViewers": 98201 },
    { "timestamp": "2024-02-02T00:00:00Z", "views": 138472, "uniqueViewers": 95123 },
    ...
  ],
  "totalViews": 3918274,
  "totalUniqueViewers": 2104839
}
```

**Notes:**
- `minute` granularity only available for the last 48 hours.
- `hour` granularity available for the last 90 days.
- `day`/`week`/`month` available for all time.
- Backed by pre-aggregated rollup tables (ClickHouse / TimescaleDB), not raw events.

---

### `GET /v1/videos/{videoId}/analytics/demographics`

View breakdown by country, device type, and traffic source.

**Request:**
```
GET /v1/videos/abc123/analytics/demographics?startDate=2024-02-01&endDate=2024-02-28
```

**Response:**
```json
HTTP/1.1 200 OK
{
  "videoId": "abc123",
  "dateRange": { "start": "2024-02-01", "end": "2024-02-28" },
  "byCountry": [
    { "country": "US", "views": 1204839, "percentage": 30.7 },
    { "country": "IN", "views": 893201, "percentage": 22.8 },
    { "country": "BR", "views": 412039, "percentage": 10.5 },
    ...
  ],
  "byDevice": [
    { "device": "mobile", "views": 2348201, "percentage": 59.9 },
    { "device": "desktop", "views": 1102384, "percentage": 28.1 },
    { "device": "tablet", "views": 312091, "percentage": 8.0 },
    { "device": "tv", "views": 155598, "percentage": 4.0 }
  ],
  "byTrafficSource": [
    { "source": "suggested", "views": 1892010, "percentage": 48.3 },
    { "source": "search", "views": 893201, "percentage": 22.8 },
    { "source": "external", "views": 612039, "percentage": 15.6 },
    { "source": "direct", "views": 312091, "percentage": 8.0 },
    { "source": "notification", "views": 208933, "percentage": 5.3 }
  ]
}
```

---

### `GET /v1/videos/{videoId}/analytics/realtime` **\***

Real-time views in the last 48 hours with minute-level granularity. The "Realtime" tab in YouTube Studio.

**Request:**
```
GET /v1/videos/abc123/analytics/realtime
```

**Response:**
```json
HTTP/1.1 200 OK
{
  "videoId": "abc123",
  "granularity": "minute",
  "windowHours": 48,
  "currentViewsPerMinute": 342,
  "peakViewsPerMinute": 12849,
  "peakTimestamp": "2024-02-27T14:23:00Z",
  "dataPoints": [
    { "timestamp": "2024-02-26T10:31:00Z", "views": 287 },
    { "timestamp": "2024-02-26T10:32:00Z", "views": 301 },
    ...
  ],
  "totalViewsLast48h": 4912837
}
```

**Notes:**
- Up to 2,880 data points (48h × 60 min/h).
- Backed by the real-time streaming pipeline (Flink → ClickHouse).
- ~30 second delay from actual view to appearance in this endpoint.

---

## 4. Trending / Leaderboard APIs

Pre-computed rankings based on view velocity. **Not** real-time aggregation.

### `GET /v1/trending` **\***

Top trending videos based on view velocity (views per hour, not total views).

**Request:**
```
GET /v1/trending?region=US&category=music&limit=50
```

**Query Parameters:**
| Parameter | Type | Required | Values | Default |
|-----------|------|----------|--------|---------|
| `region` | string | no | ISO 3166-1 alpha-2 | `global` |
| `category` | string | no | `music`, `gaming`, `sports`, `news`, `entertainment`, `all` | `all` |
| `limit` | int | no | 1-200 | 50 |
| `timeWindow` | string | no | `1h`, `6h`, `24h` | `24h` |

**Response:**
```json
HTTP/1.1 200 OK
Cache-Control: public, max-age=300
{
  "region": "US",
  "category": "music",
  "timeWindow": "24h",
  "generatedAt": "2024-02-28T10:00:00Z",
  "videos": [
    {
      "rank": 1,
      "videoId": "xyz789",
      "title": "...",
      "viewCount": 48291037,
      "viewsInWindow": 12839201,
      "viewVelocity": 534967,
      "velocityUnit": "views/hour"
    },
    ...
  ]
}
```

**Notes:**
- Pre-computed every 5-15 minutes by a batch/streaming job.
- CDN-cached with 5-min TTL.
- `viewVelocity` is the key ranking signal, not total `viewCount`.
- Regional trending considers views from that region only.

---

### `GET /v1/leaderboard/allTime`

All-time most viewed videos globally or by category.

**Request:**
```
GET /v1/leaderboard/allTime?category=music&limit=100
```

**Response:**
```json
HTTP/1.1 200 OK
Cache-Control: public, max-age=3600
{
  "category": "music",
  "generatedAt": "2024-02-28T00:00:00Z",
  "videos": [
    { "rank": 1, "videoId": "dQw4w9WgXcQ", "viewCount": 14200000000, "formattedCount": "14.2B" },
    { "rank": 2, "videoId": "abc456", "viewCount": 12100000000, "formattedCount": "12.1B" },
    ...
  ]
}
```

**Notes:**
- Recomputed daily. Heavily cached (1h TTL).
- Small, static dataset — top 200-500 videos per category.

---

## 5. Internal / Admin APIs

Authenticated internal endpoints for operations and fraud management. Not exposed to public clients.

### `POST /v1/internal/videos/{videoId}/viewCount/adjust`

Manual correction — subtract fraudulent views after bot detection.

**Request:**
```json
POST /v1/internal/videos/abc123/viewCount/adjust
Authorization: Bearer <internal-service-token>
{
  "adjustment": -142839,
  "reason": "batch_fraud_detection_run_20240228",
  "detectionJobId": "job_9283",
  "operator": "fraud-detection-pipeline",
  "approvedBy": "admin@youtube.com"
}
```

**Response:**
```json
HTTP/1.1 200 OK
{
  "videoId": "abc123",
  "previousCount": 5283901,
  "newCount": 5141062,
  "adjustment": -142839,
  "auditId": "audit_20240228_001"
}
```

**Notes:**
- Requires internal service authentication + human approval for large adjustments (> 10,000 views).
- Adjustment is atomic — view count never shows intermediate state.
- Creates an immutable audit log entry.
- Adjustment can be positive (correction of over-subtraction) or negative (fraud removal).

---

### `GET /v1/internal/videos/{videoId}/viewCount/audit`

Audit trail — history of all adjustments for a video.

**Request:**
```
GET /v1/internal/videos/abc123/viewCount/audit?limit=50
```

**Response:**
```json
HTTP/1.1 200 OK
{
  "videoId": "abc123",
  "currentCount": 5141062,
  "adjustments": [
    {
      "auditId": "audit_20240228_001",
      "timestamp": "2024-02-28T08:00:00Z",
      "adjustment": -142839,
      "reason": "batch_fraud_detection_run_20240228",
      "operator": "fraud-detection-pipeline",
      "approvedBy": "admin@youtube.com",
      "previousCount": 5283901,
      "newCount": 5141062
    },
    {
      "auditId": "audit_20240215_003",
      "timestamp": "2024-02-15T12:30:00Z",
      "adjustment": -89201,
      "reason": "manual_investigation_ticket_12345",
      "operator": "trust-safety-team",
      "approvedBy": "manager@youtube.com",
      "previousCount": 5373102,
      "newCount": 5283901
    }
  ]
}
```

---

### `PUT /v1/internal/videos/{videoId}/viewCount/freeze`

Freeze count during investigation — the view count stops updating publicly while fraud analysis runs.

**Request:**
```json
PUT /v1/internal/videos/abc123/viewCount/freeze
Authorization: Bearer <internal-service-token>
{
  "frozen": true,
  "reason": "suspected_botnet_attack",
  "investigationTicket": "TICKET-12345",
  "frozenBy": "trust-safety-team"
}
```

**Response:**
```json
HTTP/1.1 200 OK
{
  "videoId": "abc123",
  "isFrozen": true,
  "frozenAt": "2024-02-28T10:15:00Z",
  "frozenCount": 5141062,
  "reason": "suspected_botnet_attack"
}
```

**Notes:**
- When frozen, the public `GET /viewCount` endpoint returns the frozen count, not the real-time count.
- View events are still ingested and counted internally — they're just not shown publicly.
- Unfreezing (`"frozen": false`) restores the real-time count (which may be different from the frozen count, since valid views continued accumulating).

---

### `PUT /v1/internal/videos/{videoId}/viewCount/unfreeze`

Resume public count updates after investigation.

**Request:**
```json
PUT /v1/internal/videos/abc123/viewCount/unfreeze
Authorization: Bearer <internal-service-token>
{
  "adjustmentBeforeUnfreeze": -50000,
  "reason": "investigation_complete_fraud_removed",
  "investigationTicket": "TICKET-12345"
}
```

**Response:**
```json
HTTP/1.1 200 OK
{
  "videoId": "abc123",
  "isFrozen": false,
  "frozenDuration": "PT4H30M",
  "countAtFreeze": 5141062,
  "adjustmentApplied": -50000,
  "currentCount": 5298423
}
```

---

## API Summary Table

| Endpoint | Method | Latency Target | Traffic | Auth |
|----------|--------|----------------|---------|------|
| `/videos/{id}/view` | POST | < 50ms | ~800K/s | API key |
| `/videos/{id}/view/heartbeat` | POST | < 50ms | ~2M/s | API key |
| `/videos/{id}/viewCount` | GET | < 10ms | ~5M/s | Public |
| `/videos/batch/viewCounts` | GET | < 50ms | ~500K/s | Public |
| `/channels/{id}/totalViews` | GET | < 50ms | ~100K/s | Public |
| `/videos/{id}/analytics` | GET | < 2s | ~10K/s | OAuth |
| `/videos/{id}/analytics/demographics` | GET | < 2s | ~5K/s | OAuth |
| `/videos/{id}/analytics/realtime` | GET | < 500ms | ~20K/s | OAuth |
| `/trending` | GET | < 100ms | ~200K/s | Public |
| `/leaderboard/allTime` | GET | < 100ms | ~50K/s | Public |
| `/internal/.../adjust` | POST | < 500ms | ~10/s | Internal |
| `/internal/.../audit` | GET | < 500ms | ~10/s | Internal |
| `/internal/.../freeze` | PUT | < 500ms | ~1/s | Internal |
| `/internal/.../unfreeze` | PUT | < 500ms | ~1/s | Internal |

---

## Error Responses

All endpoints return standard error format:

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Too many view events from this IP for video abc123",
    "retryAfterSeconds": 60
  }
}
```

| HTTP Status | Error Code | Meaning |
|-------------|-----------|---------|
| 400 | `INVALID_REQUEST` | Malformed request body or missing required fields |
| 404 | `VIDEO_NOT_FOUND` | Video ID does not exist |
| 429 | `RATE_LIMITED` | Too many requests — back off and retry |
| 503 | `SERVICE_UNAVAILABLE` | System overloaded — retry with exponential backoff |

---

## Authentication & Authorization

| API Group | Auth Method | Who Can Access |
|-----------|------------|----------------|
| View Ingestion | API key (embedded in player) | Any client with valid player |
| View Count Reads | None (public) | Anyone |
| Analytics | OAuth 2.0 (creator token) | Video/channel owner only |
| Trending / Leaderboard | None (public) | Anyone |
| Internal / Admin | Service-to-service mTLS + human approval | Internal systems, trust & safety team |

---

*For the interview simulation discussing a subset of these APIs, see [01-interview-simulation.md](01-interview-simulation.md).*
