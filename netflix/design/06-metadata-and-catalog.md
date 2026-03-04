# Deep Dive: Metadata, Search & Catalog Systems

> **Context**: This document is a deep dive supporting the main [01-interview-simulation.md](01-interview-simulation.md). It covers how Netflix models content metadata, powers search, manages the global catalog, personalizes artwork, and annotates content at scale.

---

## Table of Contents

1. [Content Metadata Model](#1-content-metadata-model)
2. [Artwork Personalization](#2-artwork-personalization)
3. [Search Architecture](#3-search-architecture)
4. [Catalog Service](#4-catalog-service)
5. [Marken (Annotation Service)](#5-marken-annotation-service)
6. [Data Model Diagram](#6-data-model-diagram)
7. [Contrast with YouTube](#7-contrast-with-youtube)
8. [Interview Talking Points](#8-interview-talking-points)
9. [Sources](#9-sources)

---

## 1. Content Metadata Model

Netflix's metadata model is the backbone of every user-facing experience -- browsing, search, recommendations, playback, and even encoding pipeline orchestration. Every piece of content in the catalog is modeled as a rich, multi-layered entity.

### Core Entity: Title

Each title in Netflix's catalog carries a deep set of structured attributes:

| Field | Type | Description |
|-------|------|-------------|
| `titleId` | UUID / long | Globally unique identifier for this content entity |
| `type` | Enum | `MOVIE`, `SERIES`, `SEASON`, `EPISODE`, `SPECIAL` |
| `originalTitle` | String | Title in the original production language |
| `localizedTitles` | Map<Locale, String> | Title translations for 30+ languages |
| `synopsis` | Map<Locale, SynopsisBlock> | Short synopsis, long synopsis, and tagline per language |
| `cast` | List<PersonRef> | Actors with role names (e.g., "Millie Bobby Brown as Eleven") |
| `crew` | List<PersonRef> | Directors, writers, producers, cinematographers |
| `genreTags` | List<GenreTag> | Hierarchical genre taxonomy (e.g., `Action > Spy Action > Political Spy Thrillers`) |
| `maturityRating` | MaturityRating | Region-specific rating (e.g., TV-MA in US, 18 in UK, R18+ in Japan) |
| `audioTracks` | List<AudioTrack> | Language, codec (AAC, EAC-3, Dolby Atmos), channel layout (stereo, 5.1, 7.1.4) |
| `subtitleTracks` | List<SubtitleTrack> | Language, format (WebVTT, TTML), type (standard, SDH, forced narrative) |
| `releaseDate` | Date | Original release date of the content |
| `netflixReleaseDate` | Date | When Netflix made it available (may differ from theatrical release) |
| `licensingWindows` | List<LicensingWindow> | Per-region availability with start/end dates |
| `encodingProfiles` | List<EncodingProfile> | Available resolution/bitrate/codec combinations from the encoding pipeline |
| `artworkVariants` | List<ArtworkVariant> | Multiple images per title, used for personalized artwork selection |
| `runtimeMinutes` | int | Duration (for movies), average episode duration (for series) |
| `productionCountry` | List<String> | Countries of production |
| `originalLanguage` | Locale | Original language of the content |

### Series Hierarchy

For series content, the metadata forms a three-level hierarchy:

```
SERIES (e.g., "Stranger Things")
  │
  ├── SEASON 1
  │     ├── EPISODE 1 (titleId: ST-S1E1)
  │     ├── EPISODE 2 (titleId: ST-S1E2)
  │     └── ...
  │
  ├── SEASON 2
  │     ├── EPISODE 1
  │     └── ...
  │
  └── SEASON 3
        └── ...
```

Each level in the hierarchy has its own metadata:

- **Series-level**: Overall synopsis, aggregated cast across all seasons, genre tags, maturity rating
- **Season-level**: Season-specific synopsis, season-specific cast additions, season number, episode count
- **Episode-level**: Episode synopsis, episode-specific guest cast, episode title, runtime, encoding profiles, audio/subtitle tracks

**Why this hierarchy matters**: The playback path needs episode-level metadata (which encoding profiles exist, which audio tracks are available). The browse path needs series-level metadata (show the user a single card for "Stranger Things," not 34 individual episode cards). The catalog service must navigate between these levels efficiently.

### Licensing Windows

Licensing windows are one of the most operationally complex aspects of Netflix's metadata model:

```
LicensingWindow {
    titleId:      "tt-12345"
    region:       "US"
    startDate:    "2025-01-15T00:00:00Z"
    endDate:      "2027-01-14T23:59:59Z"
    rights:       [STREAMING, DOWNLOAD]
    restrictions: { maxResolution: "4K", requireDRM: true }
}
```

- A single title may have **dozens of licensing windows** -- one per region, each with different start/end dates.
- Some content is Netflix Original (perpetual license, all regions). Some is licensed from studios (specific region + date range).
- When a licensing window expires, the title must be **removed from the catalog in that region**. This is automated -- the catalog service continuously checks window expiration and removes titles at the precise moment they expire.
- When a new window opens (e.g., a studio deal makes a title available in Japan on March 1), the title must appear in the Japanese catalog at exactly midnight local time. OCAs in Japan must have the content pre-positioned before that moment.

### Audio and Subtitle Tracks

Each title can have many audio and subtitle track combinations:

```
AudioTrack {
    language:      "Japanese"
    codec:         "EAC-3"          // Enhanced AC-3 (Dolby Digital Plus)
    channels:      "5.1"            // Surround sound
    description:   false            // Not an audio description track
    bitrateKbps:   640
}

SubtitleTrack {
    language:      "English"
    format:        "WebVTT"
    type:          "SDH"            // Subtitles for the Deaf and Hard of Hearing
    forced:        false            // Not forced narrative (forced = burned-in for foreign dialogue)
}
```

A typical Netflix Original title might have:
- **30+ audio tracks**: Original language in stereo, 5.1, and Atmos; dubbed audio in 20+ languages
- **30+ subtitle tracks**: Standard subtitles, SDH, and forced narrative in multiple languages

The catalog service indexes which audio/subtitle combinations are available per title and per region, because not all dubs/subs are available in all regions.

### Maturity Rating Mapping

Maturity ratings differ by country. Netflix maintains a mapping table:

| Netflix Internal | US | UK | Japan | Germany | Brazil |
|-----------------|----|----|-------|---------|--------|
| `ALL_AGES` | TV-Y | U | G | FSK 0 | L |
| `OLDER_KIDS` | TV-Y7 | PG | PG-12 | FSK 6 | 10 |
| `TEENS` | TV-14 | 12 | R-15 | FSK 12 | 14 |
| `MATURE` | TV-MA | 18 | R-18+ | FSK 18 | 18 |

Profile-level parental controls filter content based on the maturity rating configured for that profile.

---

## 2. Artwork Personalization

Artwork personalization is one of Netflix's most innovative applications of machine learning to the metadata layer. The core insight: **different users respond to different visual representations of the same content**.

### How It Works

For every title, Netflix generates **multiple artwork variants** -- typically dozens of candidate images per title. These are not just random screenshots; they are professionally designed compositions, each emphasizing a different aspect of the content:

```
ArtworkVariant {
    titleId:       "tt-12345"
    variantId:     "aw-001"
    imageUrl:      "https://artwork.nflximg.net/tt-12345/aw-001.jpg"
    tags:          ["action", "character_focus", "dark_mood"]
    focalCharacter: "Bryan Cranston"
    dominantGenre:  "thriller"
    dimensions:     { width: 1280, height: 720 }
    cropVariants:  {
        "billboard":   "https://artwork.nflximg.net/tt-12345/aw-001-billboard.jpg",
        "portrait":    "https://artwork.nflximg.net/tt-12345/aw-001-portrait.jpg",
        "square":      "https://artwork.nflximg.net/tt-12345/aw-001-square.jpg"
    }
}
```

### Personalization Logic

The artwork selection algorithm considers the user's viewing history to pick the variant most likely to generate a click:

**Example -- "Stranger Things":**

| User Profile | Viewing History | Artwork Shown | Why |
|-------------|----------------|---------------|-----|
| Comedy fan | Watched "The Office," "Brooklyn Nine-Nine" | Artwork showing Dustin and Steve in a funny moment | Comedy-associated visual cues match user's genre affinity |
| Horror fan | Watched "The Haunting of Hill House," "Midnight Mass" | Artwork showing the Upside Down with dark, menacing lighting | Horror-associated visual cues match user's genre affinity |
| Romance fan | Watched "Bridgerton," "Emily in Paris" | Artwork showing Eleven and Mike in an emotional scene | Relationship-focused artwork matches user's affinity |

**Example -- "Pulp Fiction" (hypothetical licensed title):**

| User Profile | Artwork Shown | Reason |
|-------------|---------------|--------|
| User who watches Uma Thurman films | Artwork featuring Uma Thurman prominently | Actor affinity signal |
| User who watches John Travolta films | Artwork featuring John Travolta prominently | Actor affinity signal |
| User who watches crime thrillers | Artwork showing a tense gun scene | Genre affinity signal |

### The ML Model

Netflix uses a **contextual bandit** approach for artwork selection:

1. **Candidate generation**: For each title, the system has N artwork variants (typically 10-30+ per title).
2. **Feature extraction**: User features (genre preferences, actor affinity, recent viewing), artwork features (dominant colors, facial expressions, scene type, tagged genre associations).
3. **Scoring**: A model scores each (user, artwork) pair based on predicted probability of engagement (click, hover, play).
4. **Exploration vs exploitation**: The system uses a **Thompson Sampling** or **Upper Confidence Bound (UCB)** strategy. Most of the time it shows the highest-scoring artwork (exploitation). Occasionally it shows a different variant to gather data (exploration). This is critical -- without exploration, the model converges to a local optimum and never discovers that a new artwork variant might perform better.
5. **Measurement**: The metric is **take rate** -- the fraction of times a user who sees the title card actually clicks through to the title detail page or starts playing.

### Scale

- Netflix generates artwork variants for **every title in the catalog** (tens of thousands of titles).
- Each title has **multiple crop variants** per artwork (billboard for TV, portrait for phone, square for grid).
- The artwork selection decision happens at **home page load time** -- it must be fast. The precomputed model scores are stored in **EVCache**, and the real-time blending adds minimal latency.

### Why This Matters Architecturally

Artwork personalization demonstrates a key Netflix principle: **everything the user sees is personalized**. The home page rows, the order within rows, the artwork on each card, and even the synopsis snippet can all differ per user. This requires the metadata layer to serve not just "the metadata for title X" but "the best metadata for title X for user Y."

---

## 3. Search Architecture

Netflix's search system must handle multi-language queries across a catalog of tens of thousands of titles, with results personalized per user.

### Core Technology: Elasticsearch

Netflix's search infrastructure is built on **Elasticsearch**, chosen for its full-text search capabilities, horizontal scalability, and rich query DSL.

### Search Capabilities

| Capability | Description | Example |
|-----------|-------------|---------|
| **Prefix matching** | Match titles starting with the typed characters | "Stra" matches "Stranger Things" |
| **Fuzzy matching** | Tolerate typos using edit distance (Levenshtein distance) | "Stranegr Things" still matches "Stranger Things" |
| **Multi-language search** | Queries in any of 30+ supported languages match against localized titles | Japanese query matches Japanese title translations |
| **Entity search** | Search across titles, people (actors, directors), and genres | "Leonardo DiCaprio" returns his filmography on Netflix |
| **Typeahead / autocomplete** | Suggest completions as the user types | "Bre" suggests "Breaking Bad," "Bridgerton" |
| **Phonetic matching** | Handle pronunciation-based searches [INFERRED] | Searching by how a name sounds rather than exact spelling |

### Search Pipeline

```
User types "stranger thin"
         │
         ▼
┌─────────────────────────────┐
│  1. Query Analysis            │
│     - Language detection       │
│     - Tokenization             │
│     - Spell correction         │
│     - Query expansion          │
│       (synonyms, related)      │
└──────────────┬────────────────┘
               │
               ▼
┌─────────────────────────────┐
│  2. Multi-Index Search        │
│                               │
│  ┌─────────┐ ┌───────────┐   │
│  │ Titles   │ │ People    │   │
│  │ Index    │ │ Index     │   │
│  └────┬────┘ └─────┬─────┘   │
│       │             │         │
│  ┌────▼─────────────▼────┐   │
│  │   Merge & Deduplicate  │   │
│  └────────────┬──────────┘   │
└───────────────┼──────────────┘
                │
                ▼
┌─────────────────────────────┐
│  3. Personalized Re-ranking   │
│                               │
│  Base relevance score          │
│    + User genre affinity       │
│    + User viewing history      │
│    + Regional popularity       │
│    + Content freshness         │
│    = Personalized rank         │
└──────────────┬────────────────┘
               │
               ▼
┌─────────────────────────────┐
│  4. Response Assembly         │
│                               │
│  For each result:              │
│    - Select personalized       │
│      artwork variant           │
│    - Attach match score        │
│    - Include synopsis snippet  │
│      with highlighted match    │
└───────────────────────────────┘
```

### Personalized Re-ranking

This is the critical differentiator. The same search query returns **different result rankings for different users**:

**Example -- searching "dark":**

| User | Top Results | Why |
|------|------------|-----|
| Anime fan | "Dark Gathering," "Darker than Black" | Anime genre affinity boosts anime results |
| Thriller fan | "Dark" (German series), "Dark Waters" | Thriller affinity boosts thriller results |
| Comedy fan | "Dark comedy" genre suggestions, "After Dark" stand-up specials | Comedy affinity boosts comedy results |

The re-ranking model blends:
- **Elasticsearch relevance score** (BM25-based text matching)
- **User preference signals** from the recommendation engine (genre affinity, actor affinity, viewing recency)
- **Regional popularity** (what's trending in the user's country)
- **Content freshness** (recently added titles get a boost)

### Elasticsearch Index Design

Netflix maintains multiple indices in Elasticsearch, each optimized for a different entity type:

**Titles Index:**
```json
{
    "titleId": "tt-12345",
    "type": "SERIES",
    "titles": {
        "en": "Stranger Things",
        "ja": "ストレンジャー・シングス",
        "ko": "기묘한 이야기",
        "de": "Stranger Things"
    },
    "synopsis": {
        "en": "When a young boy vanishes, a small town uncovers..."
    },
    "cast": ["Millie Bobby Brown", "Finn Wolfhard", "Winona Ryder"],
    "genres": ["sci-fi", "horror", "drama", "teen"],
    "maturityRating": "TV-14",
    "releaseYear": 2016,
    "popularityScore": 98.5,
    "availableRegions": ["US", "GB", "JP", "DE", "BR", ...]
}
```

**People Index:**
```json
{
    "personId": "p-67890",
    "name": "Millie Bobby Brown",
    "aliases": ["ミリー・ボビー・ブラウン"],
    "roles": ["actress", "producer"],
    "knownFor": ["tt-12345", "tt-67890"],
    "popularityScore": 92.0
}
```

### Multi-Language Search Handling

Netflix supports search in 30+ languages. This requires:

1. **Language-specific analyzers**: Each language needs its own tokenizer, stemmer, and stop-word list. Japanese and Chinese need morphological analysis (not whitespace tokenization). Korean uses Hangul jamo decomposition.
2. **Transliteration**: Users might type a romanized version of a non-Latin title (e.g., "naruto" instead of "ナルト").
3. **Cross-language matching**: A user searching in English should still find content whose original title is in Korean if the English translation matches.
4. **Field boosting per language**: The user's profile language gets a boost. If a user's profile is set to Japanese, Japanese title matches score higher than English matches.

### Search Performance

- **Target latency**: < 100ms for typeahead, < 200ms for full search results
- **Caching**: Popular queries are cached in EVCache. The cache key includes the query AND the user's profile ID (because results are personalized).
- **Index updates**: When new content is added to the catalog, the Elasticsearch index is updated near-real-time (within seconds). Catalog changes propagate through a Kafka event stream that triggers index updates.

---

## 4. Catalog Service

The Catalog Service is the **central source of truth** for answering the question: "What content is available, where, and in what formats?"

### Responsibilities

```
┌──────────────────────────────────────────────────────────────┐
│                      CATALOG SERVICE                          │
│                                                               │
│  Answers:                                                     │
│    "Is title X available in region Y right now?"              │
│    "What encoding profiles exist for title X?"                │
│    "What audio/subtitle tracks are available for title X?"    │
│    "What titles are available in region Y, filtered by genre?"│
│    "When does the licensing window for title X in region Y    │
│     expire?"                                                  │
│                                                               │
│  Consumers:                                                   │
│    - Playback Service (which profiles can I stream?)          │
│    - Browse Service (what can I show this user?)              │
│    - Recommendation Service (what's in the candidate pool?)   │
│    - Search Service (what's searchable in this region?)       │
│    - Content Publishing (is this title ready for launch?)     │
│    - Download Service (can this title be downloaded offline?) │
└──────────────────────────────────────────────────────────────┘
```

### Regional Availability

The catalog is **not the same for every user**. A user in Japan sees a different catalog than a user in the United States. This is driven by licensing:

```
Title: "The Office" (US version)

  US:     Available (Netflix Original in some markets / licensed)
  Japan:  NOT available (license not acquired)
  UK:     Available (different licensing deal, different date range)
  India:  Available (different licensing deal)
```

The Catalog Service resolves regional availability in real-time:

```
Request:  GET /catalog/titles?region=JP&genre=comedy&page=1
Process:
  1. Query all comedy titles from metadata store
  2. Filter: only titles with an active licensing window for region=JP
     where window.startDate <= now AND window.endDate >= now
  3. Sort by personalized relevance (if profileId provided)
  4. Return paginated results
```

### Windowed Availability

Some titles have future licensing windows. The catalog must handle:

- **Pre-launch**: Title metadata exists in the system (for pre-positioning on OCAs, building recommendation models) but is NOT visible to users.
- **Launch moment**: At the exact start of the licensing window (often midnight local time), the title becomes visible in the regional catalog.
- **Expiration**: At the exact end of the licensing window, the title disappears from the regional catalog. Users mid-stream may be allowed to finish their current session, but new streams are blocked.

```
Timeline for "Studio Movie X" in Japan:

  ──────────────────────────────────────────────────────►  time
  │                   │                        │
  Pre-positioned      Window opens             Window closes
  on Japan OCAs       (visible, streamable)    (removed from catalog)
  (not visible)       Jan 1, 2026 00:00 JST   Dec 31, 2026 23:59 JST
```

### Data Storage

The Catalog Service uses a combination of:

- **Cassandra**: Primary store for catalog metadata. Multi-region replicated. Handles the high read throughput of millions of catalog queries per second.
- **EVCache**: Caches the resolved "available catalog for region X" to avoid re-computing licensing window checks on every request. Cache TTL is short (minutes) to handle window transitions promptly.
- **Elasticsearch**: Powers search and filtered browse queries. The ES index is a **materialized view** of the Cassandra data, updated via a Kafka-based change data capture pipeline.

### Catalog Update Pipeline

```
Content team updates metadata OR licensing window changes
         │
         ▼
┌──────────────────┐
│ Catalog Write API │
│ (updates Cassandra)│
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Change event       │
│ published to Kafka │
└────────┬─────────┘
         │
    ┌────┴──────────────────┐
    │                       │
    ▼                       ▼
┌──────────────┐    ┌──────────────────┐
│ ES Indexer    │    │ EVCache Invalidator│
│ (updates      │    │ (invalidates       │
│  search index)│    │  cached catalog)   │
└──────────────┘    └──────────────────┘
```

### Why the Catalog Service Is Critical

Every user-facing request touches the catalog:
- **Browse**: "Show me the home page" requires knowing which titles are available in the user's region.
- **Search**: Search results must only include titles available in the user's region.
- **Playback**: Before starting a stream, the playback service checks the catalog to confirm the title is available, which encoding profiles exist, and which audio/subtitle tracks are present.
- **Recommendations**: The recommendation engine's candidate pool is bounded by the catalog -- it cannot recommend a title that is not available in the user's region.

A bug in the catalog service (e.g., a title incorrectly marked as unavailable) directly impacts user experience and revenue.

---

## 5. Marken (Annotation Service)

**Marken** is Netflix's internal content annotation platform. It stores structured annotations on content entities at massive scale -- approximately **1.9 billion annotations** across Netflix's catalog.

### What Are Annotations?

Annotations are structured metadata attached to specific points or ranges within content. They go far beyond basic title-level metadata:

| Annotation Type | Description | Example |
|----------------|-------------|---------|
| **Scene boundaries** | Start/end timestamps of each scene | Scene 1: 00:00:00 - 00:03:45 (opening credits + establishing shot) |
| **Shot boundaries** | Finer-grained: start/end of each camera shot within a scene | Shot at 00:01:12 - 00:01:18 (close-up of character) |
| **Audio descriptors** | Characteristics of the audio at specific points | Loud explosion at 00:45:30, quiet dialogue at 00:12:00 |
| **Content tags** | Semantic tags used by the recommendation engine | "plot_twist_at_midpoint," "slow_burn_opening," "ensemble_cast" |
| **Visual descriptors** | What's visually happening | "dark_scene," "outdoor_daylight," "crowd_scene" |
| **Key moments** | Algorithmically or manually identified key moments | "recap_worthy_moment," "cliffhanger_ending" |
| **Nudity/violence markers** | Content advisory markers at specific timestamps | Violence from 00:32:00 to 00:34:15 |
| **Credits detection** | Where opening/closing credits start and end | Opening credits: 00:00:00 - 00:01:30 (powers "Skip Intro" button) |

### Architecture

Marken uses a polyglot persistence strategy, combining three storage systems for different access patterns:

```
┌─────────────────────────────────────────────────────────┐
│                     MARKEN SERVICE                        │
│                                                           │
│  Write Path:                Read Path:                    │
│  ┌──────────────┐          ┌────────────────────┐        │
│  │ Annotation    │          │ Query by titleId    │        │
│  │ Ingest API    │          │ + annotation type   │        │
│  └──────┬───────┘          └────────┬───────────┘        │
│         │                           │                     │
│         ▼                           ▼                     │
│  ┌──────────────┐          ┌────────────────────┐        │
│  │  Cassandra    │          │  Elasticsearch      │        │
│  │  (primary     │ ───────► │  (search + filter   │        │
│  │   store)      │  sync    │   queries)           │        │
│  └──────────────┘          └────────────────────┘        │
│         │                                                 │
│         │  batch export                                   │
│         ▼                                                 │
│  ┌──────────────┐                                        │
│  │  Apache       │                                        │
│  │  Iceberg      │                                        │
│  │  (analytics   │                                        │
│  │   + ML        │                                        │
│  │   training)   │                                        │
│  └──────────────┘                                        │
└─────────────────────────────────────────────────────────┘
```

**Why three systems?**

| System | Role | Why This System |
|--------|------|-----------------|
| **Cassandra** | Primary source of truth for annotations. Handles high write throughput from annotation pipelines. Multi-region replicated. | Annotations are written at high volume (automated pipelines process every frame). Cassandra handles write-heavy workloads with linear horizontal scaling. |
| **Elasticsearch** | Powers search and filtered queries over annotations (e.g., "find all scenes tagged as 'fight_scene' in title X"). | Cassandra is not designed for full-text or faceted queries. Elasticsearch provides the query flexibility needed for complex annotation retrieval. |
| **Apache Iceberg** | Batch analytics and ML training. Annotations are exported to Iceberg tables on S3 for offline processing. | ML training pipelines (recommendation model training, scene detection model training) need to scan billions of annotations. Iceberg provides efficient columnar reads at scale. |

### Scale

- **~1.9 billion annotations** across the Netflix catalog
- Annotations are produced by both **automated pipelines** (computer vision models that detect scenes, shots, credits) and **human annotators** (content editors who tag semantic attributes)
- Each title can have **thousands of annotations** spanning different types
- The annotation pipeline runs as part of the content ingestion DAG -- after encoding, automated annotators process the encoded output and write annotations to Marken

### How Annotations Feed Other Systems

```
Marken Annotations
    │
    ├──► Recommendation Engine
    │       "This title has slow_burn_opening + plot_twist_at_midpoint"
    │       → Match with users who like slow-burn narratives
    │
    ├──► "Skip Intro" / "Skip Recap" buttons
    │       Credits detection annotations → UI overlay at correct timestamps
    │
    ├──► Preview/Trailer Generation
    │       Key moment annotations → automated trailer assembly
    │
    ├──► Content Advisory
    │       Violence/nudity markers → maturity rating verification
    │
    └──► Encoding Optimization
            Scene complexity annotations → per-scene bitrate allocation
            (shot-based encoding uses scene boundary annotations)
```

---

## 6. Data Model Diagram

### Entity Relationship Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        NETFLIX CONTENT DATA MODEL                        │
└─────────────────────────────────────────────────────────────────────────┘

                              ┌───────────────┐
                              │    SERIES      │
                              │───────────────│
                              │ seriesId (PK)  │
                              │ originalTitle  │
                              │ genreTags[]    │
                              │ maturityRating │
                              │ productionCountry │
                              │ originalLanguage  │
                              └───────┬───────┘
                                      │ 1:N
                                      │
                              ┌───────▼───────┐
                              │    SEASON      │
                              │───────────────│
                              │ seasonId (PK)  │
                              │ seriesId (FK)  │
                              │ seasonNumber   │
                              │ episodeCount   │
                              │ releaseDate    │
                              └───────┬───────┘
                                      │ 1:N
                                      │
┌───────────────┐             ┌───────▼───────┐
│    MOVIE      │             │   EPISODE      │
│───────────────│             │───────────────│
│ titleId (PK)  │             │ titleId (PK)  │
│ originalTitle │             │ seasonId (FK) │
│ runtimeMin    │             │ episodeNumber │
│ genreTags[]   │             │ episodeTitle  │
│ maturityRating│             │ runtimeMin    │
│ releaseDate   │             │ synopsis{}    │
│ synopsis{}    │             └───────┬───────┘
└───────┬───────┘                     │
        │                             │
        └──────────┬──────────────────┘
                   │
                   │  (both Movie and Episode are "playable titles"
                   │   that share the relationships below)
                   │
        ┌──────────┼──────────────────────────────────────────┐
        │          │          │           │          │         │
        ▼          ▼          ▼           ▼          ▼         ▼
  ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌──────┐ ┌────────┐
  │ ARTWORK  │ │ AUDIO  │ │SUBTITLE│ │ENCODING│ │LICENS│ │ANNOTAT-│
  │ VARIANT  │ │ TRACK  │ │ TRACK  │ │PROFILE │ │  ING │ │  ION   │
  │──────────│ │────────│ │────────│ │────────│ │WINDOW│ │────────│
  │variantId │ │trackId │ │trackId │ │profileId│ │──────│ │annotId │
  │titleId   │ │titleId │ │titleId │ │titleId │ │windId│ │titleId │
  │(FK)      │ │(FK)    │ │(FK)    │ │(FK)    │ │titleId│ │type    │
  │imageUrl  │ │language│ │language│ │codec   │ │(FK)  │ │startTs │
  │tags[]    │ │codec   │ │format  │ │resolut.│ │region│ │endTs   │
  │focalChar │ │channels│ │type    │ │bitrate │ │start │ │payload │
  │dominantGe│ │bitrate │ │(std/   │ │resolut.│ │Date  │ │source  │
  │nre       │ │Kbps    │ │ SDH/   │ │Width   │ │end   │ │(auto/  │
  │cropVari- │ │descript│ │ forced)│ │Height  │ │Date  │ │ human) │
  │ants{}    │ │ion     │ │        │ │segmentU│ │rights│ │        │
  │          │ │(bool)  │ │        │ │rlPattern│ │[]   │ │        │
  └──────────┘ └────────┘ └────────┘ └────────┘ └──────┘ └────────┘

  1:N           1:N          1:N         1:N        1:N       1:N
  (10-30+       (30+         (30+        (~120      (dozens    (thousands
   per title)    per title)   per title)  per title) per title) per title)
```

### Relationship Summary Table

| Parent Entity | Child Entity | Cardinality | Description |
|--------------|-------------|-------------|-------------|
| Series | Season | 1:N | A series contains multiple seasons |
| Season | Episode | 1:N | A season contains multiple episodes |
| Title (Movie/Episode) | Artwork Variant | 1:N (10-30+) | Multiple personalized artwork images per playable title |
| Title (Movie/Episode) | Audio Track | 1:N (30+) | Multiple language/codec/channel combinations |
| Title (Movie/Episode) | Subtitle Track | 1:N (30+) | Multiple language/format/type combinations |
| Title (Movie/Episode) | Encoding Profile | 1:N (~120) | Multiple codec x resolution x bitrate combinations from per-title encoding |
| Title (Movie/Episode) | Licensing Window | 1:N (dozens) | Per-region availability with date ranges |
| Title (Movie/Episode) | Annotation | 1:N (thousands) | Scene boundaries, tags, audio descriptors from Marken |

### Numbers Per Title (Typical Netflix Original)

| Entity | Count per Title | Total Storage Impact |
|--------|----------------|---------------------|
| Artwork variants | 10-30+ | ~50-100 images (multiple crops each) |
| Audio tracks | 30+ | 30+ encoded audio streams |
| Subtitle tracks | 30+ | 30+ subtitle files |
| Encoding profiles | ~120 | ~120 video streams, each segmented into thousands of 2-4 sec segments |
| Licensing windows | Dozens | Metadata records (small) |
| Annotations (Marken) | Thousands | Structured metadata per scene/shot |

**Total objects per title in S3**: A single title can generate **tens of thousands of objects** (120 encoding profiles x thousands of segments per profile + audio tracks + subtitle files + artwork images).

---

## 7. Contrast with YouTube

The metadata and catalog systems reveal some of the sharpest differences between Netflix and YouTube.

### Metadata Generation

| Dimension | Netflix | YouTube |
|-----------|---------|--------|
| **Who creates metadata** | Professional content teams, automated pipelines, licensed from studios | **Uploaders** (user-generated titles, descriptions, tags, thumbnails) |
| **Quality control** | Curated: every synopsis, genre tag, and maturity rating is reviewed | Minimal curation at upload; relies on automated classifiers + community reporting |
| **Language coverage** | Netflix translates metadata into 30+ languages for every title | Uploaders write in their language; auto-translate available but not curated |
| **Artwork** | Professionally designed, multiple variants, A/B tested per user | Uploader-chosen thumbnail OR auto-generated from 3 frame options |
| **Spam/abuse risk** | None -- content comes from trusted partners | **Massive**: clickbait titles, misleading thumbnails, keyword stuffing, SEO manipulation. YouTube needs sophisticated abuse detection on metadata |

### Catalog Scope

| Dimension | Netflix | YouTube |
|-----------|---------|--------|
| **Catalog size** | Tens of thousands of titles | **800+ million videos** |
| **Content lifecycle** | Titles stay for months/years (licensing windows) | Videos stay indefinitely unless removed by uploader or policy violation |
| **Regional variation** | Heavily region-gated by licensing | Mostly globally available (except region blocks due to local law) |
| **Content model** | Hierarchical: Series > Season > Episode | Flat: Video (with playlists as optional grouping) |

### Search Differences

| Dimension | Netflix | YouTube |
|-----------|---------|--------|
| **Search importance** | Secondary discovery method (recommendations drive 75-80% of viewing) | **Primary discovery method** (search + suggested videos are the main entry points) |
| **Query types** | Title/actor/genre search | Everything: tutorials, music, news, how-to, entertainment, product reviews |
| **Result personalization** | Same query, different ranking per user based on viewing history | Same query, different ranking per user based on watch history + engagement signals |
| **Abuse resilience** | Not needed (curated metadata) | Critical: must resist keyword stuffing, fake metadata, misleading titles |
| **Index scale** | Tens of thousands of titles | **800+ million videos** with user-generated metadata -- orders of magnitude larger index |

### Social Features

| Feature | Netflix | YouTube |
|---------|---------|--------|
| **Comments** | None | Yes (massive moderation challenge) |
| **Likes/dislikes** | Thumbs up/down (feeds recommendation model, not publicly visible) | Public like/dislike counts (dislike counts hidden since 2021) |
| **Subscribe/follow** | None (no creator model) | Core feature: subscribe to channels |
| **Share** | Basic share link | Share, embed, clip, remix |
| **Community posts** | None | Yes (creators can post text, images, polls) |

YouTube's metadata system must handle social features that Netflix entirely avoids. Comments alone represent a massive metadata system -- YouTube stores and serves billions of comments, each requiring spam/abuse detection, ranking, and real-time updates.

### Summary: Why the Differences Exist

The differences all trace back to the fundamental content model:

- **Netflix = curated content, subscription model**: Netflix controls what enters the catalog. Every title is professionally produced, metadata is professionally curated, and the business goal is long-term subscriber satisfaction. There is no user-generated metadata (no comments, no user-uploaded thumbnails, no tags by users). This eliminates entire categories of problems (spam, abuse, moderation) but requires Netflix to invest in professional curation at scale.

- **YouTube = user-generated content, ad-supported model**: Anyone can upload anything. Metadata is user-generated and therefore unreliable. The business goal is maximizing engagement (watch time leads to ad impressions). YouTube must build massive infrastructure for abuse detection, content moderation, and spam filtering that Netflix simply does not need.

---

## 8. Interview Talking Points

When discussing metadata and catalog in a system design interview, these are the key points to hit at each level:

### L5 (SDE-2) Level

- "Content has metadata like title, genre, cast, and descriptions."
- "Search uses Elasticsearch."
- "Different countries have different content."

### L6 (SDE-3) Level

- Explains the full metadata model: hierarchical (series > season > episode), multi-language, licensing windows with date ranges.
- Explains artwork personalization: multiple variants per title, different users see different artwork based on viewing history, contextual bandit approach.
- Explains search architecture: Elasticsearch with personalized re-ranking, multi-language analyzers, fuzzy matching.
- Explains the catalog service as the central source of truth with regional filtering.
- Contrasts with YouTube: curated vs user-generated metadata, no spam problem, no social features.

### L7 (Principal) Level

- Discusses Marken at scale: 1.9 billion annotations, polyglot persistence (Cassandra + Elasticsearch + Iceberg), how annotations feed recommendation models and encoding optimization.
- Discusses catalog consistency challenges: what happens when a licensing window expires mid-stream, how to coordinate catalog visibility with OCA content positioning, cache invalidation strategies for catalog changes.
- Discusses the economics of artwork personalization: how much engagement lift justifies the cost of generating and serving N variants per title.
- Discusses search index maintenance at global scale: near-real-time index updates via Kafka CDC, handling schema evolution in Elasticsearch without reindexing.

---

## 9. Sources

- Netflix Tech Blog: Artwork Personalization -- "Selecting the best artwork for Netflix" and "AVA: The Art and Science of Image Discovery at Netflix"
- Netflix Tech Blog: Marken annotation service -- described in Netflix's content engineering publications
- Netflix Tech Blog: Search infrastructure -- described in Netflix's discovery and personalization publications
- Netflix Open Connect: [openconnect.netflix.com](https://openconnect.netflix.com)
- Netflix Tech Blog: EVCache -- "Caching for a Global Netflix"
- Netflix subscriber count: 301.63M paid memberships (Q4 2024, CNBC)
- Annotation count (~1.9 billion): Referenced in Netflix engineering talks on content understanding [VERIFY against latest Netflix Tech Blog publications]
