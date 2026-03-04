# Netflix Platform API Contracts

> Comprehensive reference for all Netflix platform APIs.
> Endpoints marked with a star icon are those most likely to come up in a system design interview.

---

## Table of Contents

1. [Playback APIs](#1-playback-apis)
2. [Catalog / Browse APIs](#2-catalog--browse-apis)
3. [Recommendation / Personalization APIs](#3-recommendation--personalization-apis)
4. [Search APIs](#4-search-apis)
5. [User Profile APIs](#5-user-profile-apis)
6. [Viewing History APIs](#6-viewing-history-apis)
7. [My List APIs](#7-my-list-apis)
8. [Content Ingestion APIs (Internal)](#8-content-ingestion-apis-internal)
9. [Admin / Ops APIs (Internal)](#9-admin--ops-apis-internal)
10. [Netflix vs YouTube: API Model Comparison](#10-netflix-vs-youtube-api-model-comparison)

---

## 1. Playback APIs

> These are the most critical APIs in the entire platform. Every second of video playback depends on them.

### POST /playback/start &#11088;

Initiates a playback session. This is the single most important API call in the Netflix stack. It handles license acquisition, DRM token generation, manifest resolution, and OCA (Open Connect Appliance) server selection -- all in one round-trip.

**Request:**

```json
{
  "titleId": "80100172",
  "episodeId": "80100175",          // null for movies
  "profileId": "ABCDEF123",
  "deviceId": "device-uuid-xyz",
  "deviceType": "SMART_TV",         // SMART_TV | MOBILE | WEB | GAME_CONSOLE
  "drmType": "WIDEVINE",            // WIDEVINE | FAIRPLAY | PLAYREADY
  "maxResolution": "4K",            // SD | HD | FHD | 4K | HDR10 | DOLBY_VISION
  "networkBandwidthEstimate": 25000, // kbps, client-side estimate
  "audioPreference": "ATMOS",       // STEREO | SURROUND_5_1 | ATMOS
  "subtitleLanguage": "en",
  "resumePosition": null            // null = server decides, or explicit ms offset
}
```

**Response:**

```json
{
  "sessionId": "sess-uuid-abc",
  "licenseUrl": "https://license.netflix.com/v1/wv",
  "drmToken": "eyJhbGciOiJSUzI1NiIs...",
  "drmType": "WIDEVINE",
  "manifest": {
    "url": "https://oca-east1.netflix.com/manifest/80100175.mpd",
    "format": "DASH",                // DASH | HLS
    "profiles": [
      { "bitrate": 500,  "resolution": "480p",  "codec": "h264" },
      { "bitrate": 1500, "resolution": "720p",  "codec": "h264" },
      { "bitrate": 3000, "resolution": "1080p", "codec": "h265" },
      { "bitrate": 8000, "resolution": "4K",    "codec": "h265" },
      { "bitrate": 15000,"resolution": "4K-HDR","codec": "av1"  }
    ]
  },
  "ocaServers": [
    { "url": "https://oca-east1.netflix.com", "priority": 1, "latencyMs": 5  },
    { "url": "https://oca-east2.netflix.com", "priority": 2, "latencyMs": 12 },
    { "url": "https://cdn-fallback.netflix.com", "priority": 3, "latencyMs": 40 }
  ],
  "resumePositionMs": 1245000,
  "bookmarks": {
    "introStart": 5000,
    "introEnd": 62000,
    "creditsStart": 2940000
  },
  "expiresAt": "2025-01-15T12:30:00Z"
}
```

**DRM Flow:**

```
Client                    Netflix API              License Server           OCA
  |                           |                          |                   |
  |-- POST /playback/start -->|                          |                   |
  |                           |-- validate entitlement ->|                   |
  |                           |<-- drmToken, manifest ---|                   |
  |<-- sessionId, ocaURLs ----|                          |                   |
  |                           |                          |                   |
  |-- license challenge ----->|-------forward----------->|                   |
  |<-- license response ------|<------license------------|                   |
  |                           |                          |                   |
  |-- GET segment (encrypted) --------------------------------------------->|
  |<-- encrypted video chunk <----------------------------------------------|
  |                           |                          |                   |
  [client decrypts locally using DRM license keys]
```

DRM systems by platform:
- **Widevine** -- Android, Chrome, Smart TVs (most common)
- **FairPlay** -- iOS, Safari, Apple TV
- **PlayReady** -- Windows, Xbox, some Smart TVs

---

### POST /playback/heartbeat &#11088;

Sent every 10-30 seconds during active playback. Serves three purposes: keep the session alive, report client-side health telemetry, and allow the server to push mid-stream adjustments.

**Request:**

```json
{
  "sessionId": "sess-uuid-abc",
  "currentPositionMs": 1345000,
  "currentBitrateKbps": 3000,
  "bufferHealthMs": 45000,          // how many ms of video is buffered ahead
  "droppedFrames": 2,
  "rebufferCount": 0,
  "rebufferDurationMs": 0,
  "selectedResolution": "1080p",
  "networkBandwidthEstimate": 22000,
  "playerState": "PLAYING"          // PLAYING | PAUSED | BUFFERING
}
```

**Response:**

```json
{
  "ack": true,
  "serverTimeMs": 1705312200000,
  "nextHeartbeatIntervalMs": 15000,
  "actions": []                      // e.g., ["SWITCH_OCA", "REDUCE_BITRATE"]
}
```

**Notes:**
- If the server receives no heartbeat for 2+ minutes, it assumes the session is dead and closes it.
- Buffer health below 5 seconds triggers server-side alerts; the server may respond with `SWITCH_OCA` action.
- This telemetry feeds into Netflix's real-time streaming health dashboards.

---

### POST /playback/stop &#11088;

Called when the user stops playback (closes player, switches title, or finishes the content). Records the final watch position so the user can resume later.

**Request:**

```json
{
  "sessionId": "sess-uuid-abc",
  "finalPositionMs": 2940000,
  "totalWatchTimeMs": 1695000,
  "reason": "USER_STOP",            // USER_STOP | CONTENT_END | ERROR | INACTIVITY
  "finalBitrateKbps": 3000,
  "totalRebufferDurationMs": 0,
  "totalRebufferCount": 0
}
```

**Response:**

```json
{
  "ack": true,
  "resumePositionMs": 2940000,
  "isComplete": false,               // true if >95% watched
  "nextEpisode": {                    // only for series
    "titleId": "80100176",
    "seasonNumber": 2,
    "episodeNumber": 4
  }
}
```

---

### GET /playback/resume/{titleId} &#11088;

Quick lookup for resume position without starting a full playback session. Used to display "Resume from X:XX" in the browse UI.

**Path Parameters:**
- `titleId` -- the title to check

**Query Parameters:**
- `profileId` (required)

**Response:**

```json
{
  "titleId": "80100172",
  "episodeId": "80100175",
  "resumePositionMs": 2940000,
  "totalDurationMs": 3200000,
  "percentComplete": 91.8,
  "lastWatched": "2025-01-14T22:15:00Z",
  "isComplete": false
}
```

Returns `404` if no viewing history exists for this title.

---

## 2. Catalog / Browse APIs

> The catalog is what the user browses before pressing play. Every response is personalized -- two users hitting the same endpoint see different results.

### GET /catalog/titles &#11088;

Paginated, filterable list of titles. The backbone of the browse experience.

**Query Parameters:**

| Parameter    | Type     | Description                                    |
|-------------|----------|------------------------------------------------|
| `profileId` | string   | Required. Drives personalization.              |
| `genre`     | string   | Filter by genre slug (e.g., `action`, `sci-fi`)|
| `type`      | enum     | `MOVIE` or `SERIES`                            |
| `maturity`  | enum     | `KIDS`, `TEEN`, `ADULT`                        |
| `language`  | string   | Original language filter (ISO 639-1)           |
| `page`      | int      | Page number (default: 0)                       |
| `pageSize`  | int      | Items per page (default: 40, max: 100)         |
| `sortBy`    | enum     | `RELEVANCE`, `YEAR`, `TITLE`, `RATING`         |

**Response:**

```json
{
  "page": 0,
  "pageSize": 40,
  "totalResults": 8420,
  "titles": [
    {
      "titleId": "80100172",
      "name": "Stranger Things",
      "type": "SERIES",
      "year": 2016,
      "maturityRating": "TV-14",
      "genres": ["sci-fi", "drama", "thriller"],
      "synopsis": "When a young boy vanishes...",
      "thumbnailUrl": "https://img.netflix.com/80100172/thumb.jpg",
      "backdropUrl": "https://img.netflix.com/80100172/backdrop.jpg",
      "personalizedScore": 0.95,
      "matchPercentage": 95,
      "availableResolutions": ["SD", "HD", "FHD", "4K", "HDR10"],
      "audioFormats": ["STEREO", "SURROUND_5_1", "ATMOS"],
      "hasSubtitles": true,
      "seasonCount": 4,
      "isNetflixOriginal": true
    }
  ]
}
```

**Notes:**
- `personalizedScore` is computed by the recommendation engine; it determines sort order when `sortBy=RELEVANCE`.
- The `matchPercentage` (the "98% Match" badge) is derived from this score.
- Thumbnail URLs may themselves be personalized (Netflix A/B tests artwork per user).

---

### GET /catalog/titles/{titleId} &#11088;

Full metadata for a single title. Returned when a user clicks on a title card to see the detail view.

**Response:**

```json
{
  "titleId": "80100172",
  "name": "Stranger Things",
  "type": "SERIES",
  "year": 2016,
  "endYear": null,
  "maturityRating": "TV-14",
  "maturityDescriptors": ["violence", "fear", "language"],
  "genres": ["sci-fi", "drama", "thriller"],
  "tags": ["Suspenseful", "Exciting", "Ominous"],
  "synopsis": "When a young boy vanishes, a small town uncovers...",
  "fullDescription": "In the small town of Hawkins, Indiana...",
  "cast": [
    { "name": "Millie Bobby Brown", "role": "Eleven", "order": 1 },
    { "name": "Finn Wolfhard", "role": "Mike Wheeler", "order": 2 }
  ],
  "creators": ["The Duffer Brothers"],
  "directors": [],
  "thumbnailUrl": "https://img.netflix.com/80100172/thumb.jpg",
  "backdropUrl": "https://img.netflix.com/80100172/backdrop.jpg",
  "trailerUrl": "https://cdn.netflix.com/80100172/trailer.mp4",
  "logoUrl": "https://img.netflix.com/80100172/logo.png",
  "availableResolutions": ["SD", "HD", "FHD", "4K", "HDR10"],
  "audioFormats": ["STEREO", "SURROUND_5_1", "ATMOS"],
  "subtitleLanguages": ["en", "es", "fr", "de", "ja", "ko", "pt"],
  "dubLanguages": ["en", "es", "fr", "de", "ja"],
  "isNetflixOriginal": true,
  "seasons": [
    {
      "seasonNumber": 1,
      "episodeCount": 8,
      "year": 2016,
      "episodes": [
        {
          "episodeId": "80100173",
          "episodeNumber": 1,
          "title": "Chapter One: The Vanishing of Will Byers",
          "synopsis": "On his way home from a friend's house...",
          "durationMs": 2880000,
          "thumbnailUrl": "https://img.netflix.com/80100173/thumb.jpg"
        }
      ]
    }
  ],
  "similarTitles": ["80057281", "70264888", "80025172"]
}
```

---

### GET /catalog/genres

Returns the genre taxonomy. Used to populate genre navigation menus.

**Response:**

```json
{
  "genres": [
    { "slug": "action", "displayName": "Action", "subgenres": [
        { "slug": "action-thriller", "displayName": "Action Thrillers" },
        { "slug": "action-comedy",   "displayName": "Action Comedies" }
    ]},
    { "slug": "comedy", "displayName": "Comedies", "subgenres": [] },
    { "slug": "drama",  "displayName": "Dramas",   "subgenres": [] },
    { "slug": "sci-fi", "displayName": "Sci-Fi",   "subgenres": [] },
    { "slug": "documentary", "displayName": "Documentaries", "subgenres": [] }
  ]
}
```

---

### GET /catalog/new-releases

Titles added in the last 7/30 days. Used for the "New & Popular" row.

**Query Parameters:**

| Parameter    | Type   | Description                         |
|-------------|--------|-------------------------------------|
| `profileId` | string | Required                            |
| `days`      | int    | Lookback window (default: 7)        |
| `page`      | int    | Page number                         |
| `pageSize`  | int    | Items per page                      |

**Response:** Same shape as `GET /catalog/titles`.

---

### GET /catalog/trending

Titles trending on the platform right now. Feeds the "Top 10" rows.

**Query Parameters:**

| Parameter    | Type   | Description                              |
|-------------|--------|------------------------------------------|
| `profileId` | string | Required                                 |
| `region`    | string | ISO 3166-1 country code (default: auto)  |
| `type`      | enum   | `MOVIE`, `SERIES`, or `ALL`              |

**Response:**

```json
{
  "region": "US",
  "asOf": "2025-01-15T00:00:00Z",
  "titles": [
    {
      "rank": 1,
      "titleId": "81234567",
      "name": "The Night Agent",
      "type": "SERIES",
      "thumbnailUrl": "https://img.netflix.com/81234567/thumb.jpg",
      "viewsLast7Days": 85000000,
      "weeklyRank": 1,
      "weeksInTop10": 3
    }
  ]
}
```

---

## 3. Recommendation / Personalization APIs

> Netflix estimates 75-80% of all viewing is driven by recommendations, not search. These APIs are the revenue engine.

### GET /recommendations/home &#11088;

Returns the personalized home screen: an ordered list of "rows," each row being a themed list of titles. This is the most complex API response in the system because every field -- row order, row composition, even artwork -- is personalized.

**Query Parameters:**

| Parameter    | Type   | Description                           |
|-------------|--------|---------------------------------------|
| `profileId` | string | Required                              |
| `deviceType`| enum   | Affects number of rows/items returned |
| `maxRows`   | int    | Max number of rows (default: 40)      |

**Response:**

```json
{
  "profileId": "ABCDEF123",
  "generatedAt": "2025-01-15T10:30:00Z",
  "rows": [
    {
      "rowId": "continue-watching",
      "displayName": "Continue Watching",
      "rowType": "CONTINUE_WATCHING",
      "personalizedRank": 1,
      "titles": [
        {
          "titleId": "80100172",
          "name": "Stranger Things",
          "thumbnailUrl": "https://img.netflix.com/80100172/personalized-art-v3.jpg",
          "resumePositionMs": 2940000,
          "totalDurationMs": 3200000,
          "episodeInfo": "S2:E3"
        }
      ]
    },
    {
      "rowId": "trending-now",
      "displayName": "Trending Now",
      "rowType": "TRENDING",
      "personalizedRank": 2,
      "titles": []
    },
    {
      "rowId": "because-you-watched-80100172",
      "displayName": "Because You Watched Stranger Things",
      "rowType": "BECAUSE_YOU_WATCHED",
      "seedTitleId": "80100172",
      "personalizedRank": 3,
      "titles": []
    },
    {
      "rowId": "top-picks",
      "displayName": "Top Picks for You",
      "rowType": "TOP_PICKS",
      "personalizedRank": 4,
      "titles": []
    }
  ]
}
```

**Row types:**

| Row Type               | Description                                     |
|------------------------|-------------------------------------------------|
| `CONTINUE_WATCHING`    | Titles the user has started but not finished     |
| `TRENDING`             | Popular in the user's region                     |
| `TOP_PICKS`            | Highest predicted match scores                   |
| `BECAUSE_YOU_WATCHED`  | Similar to a specific recently watched title     |
| `NEW_RELEASES`         | Recently added, filtered by user taste           |
| `GENRE`                | Genre-specific (e.g., "Sci-Fi & Fantasy")        |
| `NETFLIX_ORIGINALS`    | Original content, personalized order             |
| `WATCH_AGAIN`          | Previously completed titles                      |

---

### GET /recommendations/similar/{titleId} &#11088;

Returns titles similar to a given title. Powers the "More Like This" tab on the detail page.

**Path Parameters:**
- `titleId` -- the seed title

**Query Parameters:**

| Parameter    | Type   | Description                |
|-------------|--------|----------------------------|
| `profileId` | string | Required                   |
| `limit`     | int    | Max results (default: 30)  |

**Response:**

```json
{
  "seedTitleId": "80100172",
  "titles": [
    {
      "titleId": "80057281",
      "name": "Dark",
      "matchScore": 0.92,
      "matchPercentage": 92,
      "thumbnailUrl": "https://img.netflix.com/80057281/thumb.jpg",
      "similarityReasons": ["genre-overlap", "mood-match", "collaborative-filtering"]
    }
  ]
}
```

---

### GET /recommendations/continue-watching &#11088;

Dedicated endpoint for the "Continue Watching" row. Separated from the home endpoint because it needs to be fresher (updated on every playback stop) and is fetched independently for quick rendering.

**Query Parameters:**

| Parameter    | Type   | Description |
|-------------|--------|-------------|
| `profileId` | string | Required    |

**Response:**

```json
{
  "titles": [
    {
      "titleId": "80100172",
      "episodeId": "80100175",
      "name": "Stranger Things",
      "episodeTitle": "Chapter Three: The Pollywog",
      "seasonNumber": 2,
      "episodeNumber": 3,
      "resumePositionMs": 2940000,
      "totalDurationMs": 3200000,
      "percentComplete": 91.8,
      "thumbnailUrl": "https://img.netflix.com/80100175/thumb.jpg",
      "lastWatched": "2025-01-14T22:15:00Z"
    }
  ]
}
```

---

## 4. Search APIs

### GET /search?q={query}

Full-text search across titles, actors, directors, and genres. Results are personalized -- the same query by two users may produce different rankings.

**Query Parameters:**

| Parameter    | Type   | Description                          |
|-------------|--------|--------------------------------------|
| `q`         | string | Search query (required, min 1 char)  |
| `profileId` | string | Required (drives personalized rank)  |
| `type`      | enum   | `MOVIE`, `SERIES`, or `ALL`          |
| `page`      | int    | Page number                          |
| `pageSize`  | int    | Items per page (default: 20)         |

**Response:**

```json
{
  "query": "stranger",
  "totalResults": 15,
  "results": [
    {
      "titleId": "80100172",
      "name": "Stranger Things",
      "type": "SERIES",
      "year": 2016,
      "matchType": "TITLE",           // TITLE | ACTOR | DIRECTOR | GENRE
      "matchField": "name",
      "personalizedScore": 0.95,
      "thumbnailUrl": "https://img.netflix.com/80100172/thumb.jpg"
    },
    {
      "titleId": "70153404",
      "name": "Doctor Strange",
      "type": "MOVIE",
      "year": 2016,
      "matchType": "TITLE",
      "matchField": "name",
      "personalizedScore": 0.72,
      "thumbnailUrl": "https://img.netflix.com/70153404/thumb.jpg"
    }
  ],
  "relatedGenres": ["sci-fi", "thriller"],
  "didYouMean": null
}
```

---

### GET /search/suggestions?q={prefix}

Typeahead / autocomplete suggestions. Returns results as the user types, typically after 2+ characters.

**Query Parameters:**

| Parameter    | Type   | Description                  |
|-------------|--------|------------------------------|
| `q`         | string | Prefix query (required)      |
| `profileId` | string | Required                     |
| `limit`     | int    | Max suggestions (default: 8) |

**Response:**

```json
{
  "prefix": "stra",
  "suggestions": [
    { "text": "Stranger Things",  "type": "TITLE",  "titleId": "80100172" },
    { "text": "Stray",            "type": "TITLE",  "titleId": "81445678" },
    { "text": "Stratton",         "type": "TITLE",  "titleId": "80178943" },
    { "text": "Meryl Streep",     "type": "ACTOR",  "personId": "p-20001"  },
    { "text": "Strategy Games",   "type": "GENRE",  "genreSlug": "strategy-games" }
  ]
}
```

**Notes:**
- Backed by a prefix trie or Elasticsearch prefix query.
- Latency target: < 50ms at p99 (users are typing; every keystroke sends a request).
- Suggestions are personalized: a drama fan sees drama titles ranked higher.

---

## 5. User Profile APIs

### GET /profiles

Returns all profiles for the authenticated account.

**Response:**

```json
{
  "accountId": "acct-123456",
  "maxProfiles": 5,
  "profiles": [
    {
      "profileId": "ABCDEF123",
      "name": "Ashwani",
      "avatarUrl": "https://img.netflix.com/avatars/robot.png",
      "isKids": false,
      "maturityLevel": "ADULT",
      "language": "en",
      "createdAt": "2020-03-15T10:00:00Z"
    },
    {
      "profileId": "GHIJKL456",
      "name": "Kids",
      "avatarUrl": "https://img.netflix.com/avatars/panda.png",
      "isKids": true,
      "maturityLevel": "KIDS",
      "language": "en",
      "createdAt": "2020-03-15T10:05:00Z"
    }
  ]
}
```

---

### POST /profiles

Create a new profile (max 5 per account).

**Request:**

```json
{
  "name": "Guest",
  "avatarId": "avatar-42",
  "isKids": false,
  "language": "en"
}
```

**Response:** `201 Created` with the full profile object.

**Error:** `409 Conflict` if the account already has 5 profiles.

---

### PUT /profiles/{profileId}

Update a profile's settings.

**Request:**

```json
{
  "name": "New Name",
  "avatarId": "avatar-55",
  "language": "es",
  "maturityLevel": "TEEN",
  "autoplayNextEpisode": true,
  "autoplayPreviews": false
}
```

**Response:** `200 OK` with the updated profile object.

---

### DELETE /profiles/{profileId}

Delete a profile and all associated data (viewing history, ratings, my list).

**Response:** `204 No Content`

**Note:** The primary profile (first profile on the account) cannot be deleted.

---

## 6. Viewing History APIs

### GET /history/{profileId}

Returns the viewing history for a profile, ordered by most recently watched.

**Query Parameters:**

| Parameter  | Type | Description                    |
|-----------|------|--------------------------------|
| `page`    | int  | Page number                    |
| `pageSize`| int  | Items per page (default: 20)   |

**Response:**

```json
{
  "profileId": "ABCDEF123",
  "totalItems": 342,
  "items": [
    {
      "titleId": "80100172",
      "episodeId": "80100175",
      "name": "Stranger Things",
      "episodeTitle": "Chapter Three: The Pollywog",
      "watchedAt": "2025-01-14T22:15:00Z",
      "durationWatchedMs": 1695000,
      "totalDurationMs": 3200000,
      "percentComplete": 52.9,
      "deviceType": "SMART_TV"
    }
  ]
}
```

---

### DELETE /history/{profileId}/{titleId}

Remove a title from viewing history. This also removes it from "Continue Watching" and resets recommendation signals for that title.

**Response:** `204 No Content`

---

### POST /history/{profileId}/rate

Submit a rating (thumbs up / thumbs down, or the newer two-thumbs-up).

**Request:**

```json
{
  "titleId": "80100172",
  "rating": "THUMBS_UP"            // THUMBS_UP | THUMBS_DOWN | DOUBLE_THUMBS_UP | CLEAR
}
```

**Response:**

```json
{
  "ack": true,
  "titleId": "80100172",
  "rating": "THUMBS_UP",
  "ratedAt": "2025-01-15T10:30:00Z"
}
```

**Notes:**
- Ratings feed directly into the recommendation model.
- `DOUBLE_THUMBS_UP` signals a "loved it" and boosts similar content more aggressively.
- `CLEAR` removes a previous rating.

---

## 7. My List APIs

### GET /mylist/{profileId}

Returns the user's "My List" (manually saved titles for later viewing).

**Query Parameters:**

| Parameter  | Type | Description          |
|-----------|------|----------------------|
| `page`    | int  | Page number          |
| `pageSize`| int  | Items per page       |
| `sortBy`  | enum | `ADDED_DATE`, `TITLE`, `RELEVANCE` |

**Response:**

```json
{
  "profileId": "ABCDEF123",
  "totalItems": 47,
  "items": [
    {
      "titleId": "80100172",
      "name": "Stranger Things",
      "type": "SERIES",
      "thumbnailUrl": "https://img.netflix.com/80100172/thumb.jpg",
      "addedAt": "2025-01-10T08:00:00Z"
    }
  ]
}
```

---

### POST /mylist/{profileId}/{titleId}

Add a title to My List.

**Response:** `201 Created`

```json
{
  "ack": true,
  "titleId": "80100172",
  "addedAt": "2025-01-15T10:35:00Z",
  "myListSize": 48
}
```

---

### DELETE /mylist/{profileId}/{titleId}

Remove a title from My List.

**Response:** `204 No Content`

---

## 8. Content Ingestion APIs (Internal)

> These APIs are internal to Netflix Studios and the content engineering pipeline. Not exposed to end users, but critical to understand for system design interviews.

### POST /ingest/upload &#11088;

Uploads raw content (mezzanine files). Supports chunked, resumable uploads because source files are often 100+ GB.

**Request Headers:**

```
Content-Type: application/octet-stream
X-Upload-Id: upload-uuid-xyz
X-Chunk-Index: 0
X-Total-Chunks: 150
X-Checksum-SHA256: abc123...
```

**Request Body:** Raw binary chunk data.

**Response:**

```json
{
  "uploadId": "upload-uuid-xyz",
  "chunkIndex": 0,
  "received": true,
  "chunksReceived": 1,
  "totalChunks": 150,
  "percentComplete": 0.67
}
```

**Notes:**
- Mezzanine files are the highest-quality source masters (ProRes, uncompressed).
- A single movie may be 200-500 GB at mezzanine quality.
- Uploads go to S3 first, then are pulled by the transcoding pipeline.
- Resumable: if a chunk fails, re-upload only that chunk.

---

### POST /ingest/transcode &#11088;

Triggers the transcoding pipeline for an uploaded asset. Netflix transcodes each title into hundreds of renditions (resolution x bitrate x codec x audio format).

**Request:**

```json
{
  "uploadId": "upload-uuid-xyz",
  "titleId": "81999999",
  "contentType": "MOVIE",
  "outputProfiles": [
    { "resolution": "480p",  "bitrate": 500,   "codec": "h264" },
    { "resolution": "720p",  "bitrate": 1500,  "codec": "h264" },
    { "resolution": "1080p", "bitrate": 3000,  "codec": "h265" },
    { "resolution": "1080p", "bitrate": 4500,  "codec": "av1"  },
    { "resolution": "4K",    "bitrate": 8000,  "codec": "h265" },
    { "resolution": "4K",    "bitrate": 12000, "codec": "av1"  },
    { "resolution": "4K-HDR","bitrate": 15000, "codec": "av1"  }
  ],
  "audioProfiles": [
    { "format": "AAC",          "channels": "STEREO" },
    { "format": "EAC3",         "channels": "SURROUND_5_1" },
    { "format": "DOLBY_ATMOS",  "channels": "ATMOS" }
  ],
  "subtitleTracks": ["en", "es", "fr", "de", "ja", "ko"],
  "priority": "STANDARD"            // STANDARD | HIGH | URGENT
}
```

**Response:**

```json
{
  "jobId": "transcode-job-uuid-abc",
  "status": "QUEUED",
  "estimatedCompletionMinutes": 240,
  "totalRenditions": 21,
  "createdAt": "2025-01-15T11:00:00Z"
}
```

**Notes:**
- Netflix uses per-shot encoding (each shot is analyzed independently for optimal bitrate).
- A single title generates ~1,200 files when you include all resolution/codec/audio/subtitle combinations.
- Transcoding runs on a massive compute cluster; a single movie takes 2-6 hours.

---

### GET /ingest/jobs/{jobId}

Poll the status of a transcoding job.

**Response:**

```json
{
  "jobId": "transcode-job-uuid-abc",
  "status": "IN_PROGRESS",          // QUEUED | IN_PROGRESS | COMPLETED | FAILED
  "progress": {
    "totalRenditions": 21,
    "completedRenditions": 14,
    "failedRenditions": 0,
    "percentComplete": 66.7
  },
  "renditions": [
    {
      "profile": "1080p/3000/h265",
      "status": "COMPLETED",
      "outputUrl": "s3://netflix-content/81999999/1080p_3000_h265.mp4",
      "fileSizeMB": 4200,
      "vmafScore": 93.2
    }
  ],
  "startedAt": "2025-01-15T11:02:00Z",
  "estimatedCompletionAt": "2025-01-15T15:00:00Z"
}
```

**Notes:**
- `vmafScore` is Netflix's own video quality metric (0-100). Scores above 90 are considered excellent.
- Failed renditions are automatically retried up to 3 times.

---

### POST /ingest/publish &#11088;

Makes a title available for streaming. Pushes transcoded content to OCA servers worldwide.

**Request:**

```json
{
  "titleId": "81999999",
  "jobId": "transcode-job-uuid-abc",
  "publishDate": "2025-02-01T00:00:00Z",  // scheduled release, or null for immediate
  "regions": ["US", "CA", "GB", "DE", "JP", "ALL"],
  "metadata": {
    "name": "New Movie Title",
    "synopsis": "A gripping story about...",
    "genres": ["drama", "thriller"],
    "maturityRating": "TV-MA",
    "cast": [],
    "trailerUrl": "s3://netflix-content/81999999/trailer.mp4"
  }
}
```

**Response:**

```json
{
  "publishId": "pub-uuid-xyz",
  "status": "SCHEDULED",             // SCHEDULED | PROPAGATING | LIVE
  "publishDate": "2025-02-01T00:00:00Z",
  "ocaPropagation": {
    "totalOcaServers": 17000,
    "serversReady": 0,
    "estimatedPropagationHours": 48
  }
}
```

**Notes:**
- Content is pushed to ~17,000 OCA servers globally before the publish date.
- High-demand titles (new seasons of popular series) are pre-positioned on more OCA servers.
- Propagation takes 24-48 hours for global availability.

---

## 9. Admin / Ops APIs (Internal)

### GET /health

Standard health check endpoint. Used by load balancers and monitoring systems.

**Response:**

```json
{
  "status": "UP",
  "timestamp": "2025-01-15T10:30:00Z",
  "version": "2025.01.15-build-4521",
  "dependencies": {
    "database": "UP",
    "cache": "UP",
    "recommendations": "UP",
    "playback": "UP"
  }
}
```

---

### GET /metrics

Exposes Prometheus-compatible metrics for monitoring and alerting.

**Response (text/plain, Prometheus format):**

```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="GET",endpoint="/catalog/titles",status="200"} 1.2e+09

# HELP playback_start_latency_ms Playback start latency
# TYPE playback_start_latency_ms histogram
playback_start_latency_ms_bucket{le="100"} 850000
playback_start_latency_ms_bucket{le="500"} 980000
playback_start_latency_ms_bucket{le="1000"} 999000

# HELP active_streams Current active streaming sessions
# TYPE active_streams gauge
active_streams 8500000

# HELP rebuffer_rate Rebuffer events per stream-hour
# TYPE rebuffer_rate gauge
rebuffer_rate 0.02
```

---

### POST /config/feature-flags

Update feature flags for A/B testing and gradual rollouts.

**Request:**

```json
{
  "flagName": "new-recommendation-algo-v2",
  "enabled": true,
  "rolloutPercentage": 10,
  "targetCriteria": {
    "regions": ["US"],
    "deviceTypes": ["WEB"],
    "accountAgeMinDays": 30
  }
}
```

**Response:**

```json
{
  "flagName": "new-recommendation-algo-v2",
  "enabled": true,
  "rolloutPercentage": 10,
  "updatedAt": "2025-01-15T10:35:00Z",
  "updatedBy": "eng-user-42"
}
```

---

### POST /cache/invalidate/{titleId}

Invalidates all cached data for a title across CDN, application caches, and client caches. Used when metadata is updated, content is pulled, or regional availability changes.

**Response:**

```json
{
  "titleId": "80100172",
  "invalidated": true,
  "cacheLayersCleared": ["CDN", "APPLICATION", "CLIENT_HINT"],
  "propagationEstimateSeconds": 30
}
```

---

## 10. Netflix vs YouTube: API Model Comparison

The API surface of Netflix and YouTube reflects fundamentally different business models and content strategies.

### Content Model

| Dimension              | Netflix                                      | YouTube                                        |
|------------------------|----------------------------------------------|------------------------------------------------|
| Content source         | Licensed and original content                | User-generated + premium (YouTube Originals)   |
| Upload API             | Internal only (`POST /ingest/upload`)        | Public API (`POST /youtube/v3/videos`)         |
| Upload volume          | Hundreds of titles per week                  | 500+ hours of video uploaded every minute      |
| Content curation       | Human-curated + algorithmic                  | Almost entirely algorithmic                    |
| Moderation             | Pre-publish review (all content is vetted)   | Post-publish moderation (ContentID, ML flags)  |

### API Surface Differences

| Feature                | Netflix                                      | YouTube                                        |
|------------------------|----------------------------------------------|------------------------------------------------|
| Public upload API      | Does not exist                               | Core feature of the platform                   |
| Comments API           | Does not exist (no comments on Netflix)      | Full CRUD (`/commentThreads`, `/comments`)     |
| Like/Dislike           | Thumbs up/down (private, feeds rec engine)   | Public like count (social signal)              |
| Subscribe/Follow       | No concept of subscribing to creators        | Core engagement loop (`/subscriptions`)        |
| Live streaming         | Very limited (live events only)              | YouTube Live, Super Chat, real-time chat       |
| Ads API                | Does not exist (ad-free tier) or limited     | Massive ad platform (`/youtube/v3/ads`)        |
| Channel API            | Does not exist                               | Channels, playlists, community posts           |
| Monetization API       | N/A (subscription model)                     | YouTube Partner Program, revenue sharing       |

### Scale Comparison

| Metric                         | Netflix                        | YouTube                            |
|--------------------------------|--------------------------------|------------------------------------|
| Concurrent streams             | ~10 million peak               | ~500 million concurrent viewers    |
| Content library size           | ~15,000 titles                 | ~800 million videos                |
| Daily uploads                  | 10-50 titles                   | ~720,000 hours of video            |
| API calls / second (estimated) | ~1 million                     | ~10 million+                       |
| Recommendation complexity      | Deep personalization per user  | Engagement-optimized ranking       |

### Optimization Philosophy

**Netflix optimizes for satisfaction.** The goal is to make users feel their subscription is worth it. Metrics that matter: retention rate, hours watched (but not at the expense of satisfaction), completion rate. A user who watches 2 hours of content they love is more valuable than one who watches 8 hours of content they feel "meh" about.

**YouTube optimizes for engagement.** The goal is to maximize time on platform (which drives ad revenue). Metrics that matter: watch time, click-through rate, session duration. The recommendation algorithm actively tries to keep users watching as long as possible.

This fundamental difference manifests in the APIs:
- Netflix has no concept of "trending comments" or "most liked" -- social engagement features are absent because they do not drive subscriptions.
- YouTube's API is heavily oriented around social features (comments, likes, shares, subscriptions) because social engagement drives return visits and ad impressions.
- Netflix invests heavily in the playback quality APIs (heartbeat, OCA selection, adaptive bitrate) because a single buffering event can cancel a subscription. YouTube tolerates more quality variance because the content is free.
