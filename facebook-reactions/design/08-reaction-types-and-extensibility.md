# Reaction Type System and Extensibility

## 1. Current Reaction Types

Facebook's reaction system evolved from a single "Like" button (2009) to a curated set of emotional responses.

| Reaction | Emoji | Introduced | Notes |
|----------|-------|------------|-------|
| Like     | :thumbsup:     | 2009       | Original reaction; the only option for 7 years |
| Love     | :heart:     | Feb 24, 2016 | Part of the initial 5-reaction expansion |
| Haha     | :joy:     | Feb 24, 2016 | Part of the initial 5-reaction expansion |
| Wow      | :open_mouth:     | Feb 24, 2016 | Part of the initial 5-reaction expansion |
| Sad      | :cry:     | Feb 24, 2016 | Part of the initial 5-reaction expansion |
| Angry    | :rage:     | Feb 24, 2016 | Part of the initial 5-reaction expansion |
| Care     | :hugs:     | 2020         | Added during COVID-19; initially temporary, later made permanent |

**Total: 7 active reaction types** across 3.07B monthly active users.

### The "Yay" Reaction That Never Shipped

A "Yay" reaction (a celebratory star character) was tested in October 2015 in Ireland and Spain. It was cut before the global launch because it was too vague -- users in different cultures interpreted it differently, and it overlapped semantically with "Haha" and "Love." This is a key lesson: **reactions must be universally unambiguous across all markets**.

---

## 2. Data Model Options for Extensibility

The central question: how do you store `reaction_type` in a system with trillions of rows while retaining the ability to add new types?

### Option A: Enum Column

```sql
CREATE TABLE reactions (
    user_id     BIGINT,
    entity_id   BIGINT,
    reaction_type ENUM('like','love','haha','wow','sad','angry','care'),
    created_at  TIMESTAMP,
    PRIMARY KEY (entity_id, user_id)
);
```

| Aspect | Detail |
|--------|--------|
| **Storage** | Internally stored as 1-2 byte integer; very compact |
| **Type safety** | Enforced at the database level; invalid values rejected |
| **Adding a new type** | Requires `ALTER TABLE ... MODIFY COLUMN` on a trillion-row table |
| **Migration cost** | Days to weeks even with online DDL tools (e.g., pt-online-schema-change, gh-ost) |
| **Risk** | Schema migration on the hottest table in the system is operationally dangerous |

**Verdict: Not suitable at Facebook's scale.** The cost and risk of schema changes on a table with trillions of rows makes this impractical for a system that may need to add temporary or permanent reaction types.

---

### Option B: Integer/String Type Column

```sql
CREATE TABLE reactions (
    user_id       BIGINT,
    entity_id     BIGINT,
    reaction_type TINYINT,       -- 1=like, 2=love, 3=haha, ...
    created_at    TIMESTAMP,
    PRIMARY KEY (entity_id, user_id)
);
```

Or with a string:

```sql
    reaction_type VARCHAR(16)    -- 'like', 'love', 'haha', ...
```

| Aspect | Detail |
|--------|--------|
| **Storage** | TINYINT = 1 byte (up to 255 types). VARCHAR(16) = 1-17 bytes |
| **Type safety** | None at database level; application must validate |
| **Adding a new type** | Code change only (add constant/mapping); zero schema migration |
| **Migration cost** | None |
| **Risk** | Low; a bad value could be written if validation has a bug |

**Verdict: Good balance of extensibility and simplicity.** TINYINT is preferred over VARCHAR for storage efficiency at scale. The mapping from integer to display name lives in application code or a configuration service.

---

### Option C: Type Registry Table

```sql
CREATE TABLE reaction_types (
    type_id        TINYINT PRIMARY KEY,
    type_name      VARCHAR(32) NOT NULL,     -- 'like', 'love', ...
    emoji          VARCHAR(8),               -- unicode emoji or asset key
    display_order  SMALLINT,                 -- order in the reaction picker UI
    sentiment      ENUM('positive','negative','neutral'),
    is_active      BOOLEAN DEFAULT TRUE,
    available_from DATE,
    available_to   DATE,                     -- NULL = permanent
    created_at     TIMESTAMP
);

CREATE TABLE reactions (
    user_id       BIGINT,
    entity_id     BIGINT,
    reaction_type TINYINT,                   -- FK to reaction_types.type_id
    created_at    TIMESTAMP,
    PRIMARY KEY (entity_id, user_id)
);
```

| Aspect | Detail |
|--------|--------|
| **Storage** | Same as Option B (TINYINT in the hot table) |
| **Type safety** | Foreign key constraint + application validation |
| **Adding a new type** | `INSERT INTO reaction_types` -- instant, no migration |
| **Migration cost** | None |
| **Metadata** | Rich: sentiment, active/inactive, availability window, display order |
| **Read overhead** | Extra JOIN on registry table (trivially cacheable -- only ~10 rows) |

**Verdict: Most flexible. This is the most likely approach at Facebook's scale.** The registry table is tiny (single-digit rows), easily cached in memory on every application server, and provides a single source of truth for all reaction metadata. The hot `reactions` table stores only an integer `type_id`.

---

### Comparison Summary

| Criteria | Enum Column | Integer/String | Type Registry |
|----------|-------------|----------------|---------------|
| Storage efficiency | Excellent (1-2 bytes) | Excellent (1 byte TINYINT) | Excellent (1 byte TINYINT) |
| Type safety | Database-enforced | Application-enforced | FK + application-enforced |
| Schema migration to add type | Required (days/weeks) | Not required | Not required |
| Metadata per type | None | None (in code) | Rich (table columns) |
| Temporary/event reactions | Very hard | Manual (code flags) | Native (available_from/to) |
| Operational risk | High | Low | Low |
| **Recommended at scale** | **No** | **Acceptable** | **Yes** |

---

## 3. Client-Server Contract

Adding a new reaction type must not break the billions of existing client installations that have not yet updated.

### Protocol Design

```
Server response for a reaction:
{
    "typeId": 7,
    "typeName": "care",
    "displayName": "Care",
    "emoji": "🤗",
    "fallbackLabel": "Reacted"
}
```

### Backward Compatibility Rules

| Rule | Description |
|------|-------------|
| **Unknown type graceful fallback** | Old clients that don't recognize `typeId: 7` render the `fallbackLabel` ("Reacted") instead of crashing |
| **Server always sends fallback** | Every reaction response includes both `typeId` and `fallbackLabel` so clients of any version can render something |
| **Client-side type registry** | Clients fetch the full reaction type registry on app launch or periodically (e.g., every 24 hours) |
| **Feature flags per region** | New reactions can be rolled out to specific regions or a percentage of users before global launch |
| **Version gating** | Server can filter available reactions based on client version header |

### Rollout Sequence for a New Reaction

```
1. INSERT new type into registry (is_active = false)
2. Deploy server code that recognizes the new typeId
3. Ship client update with new emoji asset and animation
4. Enable feature flag for internal employees (dogfood)
5. Gradually ramp: 1% -> 10% -> 50% -> 100% by region
6. Set is_active = true globally
```

At no point during this sequence do old clients break. They simply don't see the new reaction in their picker, and they render any new-type reactions from other users as "Reacted."

---

## 4. Temporary and Event Reactions

Facebook has experimented with time-limited reactions tied to cultural moments or global events.

| Reaction | Event | Period | Outcome |
|----------|-------|--------|---------|
| Pride (rainbow flag) | Pride Month | June (various years) | Available for one month |
| Care | COVID-19 pandemic | April 2020 | Initially temporary; made permanent due to sustained usage |
| Thankful (flower) | Mother's Day 2016 | May 2016 | Removed after the event |

### Implementation

The type registry naturally supports this:

```sql
INSERT INTO reaction_types (type_id, type_name, is_active, available_from, available_to)
VALUES (8, 'pride', TRUE, '2024-06-01', '2024-06-30');
```

### Key Design Rules for Temporary Reactions

1. **Never delete user data.** When a temporary reaction's availability window ends, the reaction picker stops showing it for NEW reactions. But existing reactions of that type remain visible and countable.
2. **Users can remove but not re-add.** If a user had reacted with a now-expired type, they can remove it, but they cannot add it again.
3. **Counts persist.** Aggregation counters for expired types remain in cache and are displayed normally.
4. **Gradual rollout by region.** Temporary reactions can be targeted by region using feature flags -- e.g., a Diwali reaction available only in India.

```
Timeline:
|-- available_from --|---- active window ----|-- available_to --|
                     Users CAN add reaction    Users CANNOT add
                                                Existing reactions
                                                remain visible
```

---

## 5. Impact on Counts and Aggregation

The number of possible reaction types directly affects the aggregation and caching strategy.

### Fixed Small Set (Facebook's approach: 7 types)

```
Cache entry per entity:
{
    "entity_id": 12345,
    "counts": {
        "like": 1042,
        "love": 318,
        "haha": 87,
        "wow": 12,
        "sad": 5,
        "angry": 3,
        "care": 41
    },
    "total": 1508
}
```

| Property | Value |
|----------|-------|
| Counters per entity | 7 (fixed, predictable) |
| Cache entry size | ~100 bytes (fits in a single cache line) |
| Sentiment analysis | Tractable: like/love/care = positive, sad/angry = negative, haha/wow = neutral/context-dependent |
| Pre-aggregation | Feasible: 7 counters can be maintained in real time |
| UI rendering | Deterministic: show top 3 reaction emojis + total count |

### Arbitrary Set (Slack's approach: any emoji)

```
Cache entry per message:
{
    "message_id": 67890,
    "counts": {
        "👍": 5,
        "🎉": 3,
        "👀": 2,
        "🚀": 1,
        "custom_shipit": 1,
        "🤔": 1,
        ... potentially hundreds more
    }
}
```

| Property | Value |
|----------|-------|
| Counters per entity | Unbounded (could be 1 or 500) |
| Cache entry size | Variable, potentially kilobytes |
| Sentiment analysis | Extremely difficult with arbitrary/custom emojis |
| Pre-aggregation | Not feasible for all possible emojis; must aggregate on read |
| UI rendering | Complex: need overflow UI ("and 47 more reactions...") |

### Why Facebook Chose a Fixed Set

At 3.07B MAU generating billions of reactions daily, a fixed small set means:
- **Predictable cache size:** Every entity has the same counter structure.
- **Atomic counter updates:** Incrementing one of 7 counters is a simple, lock-free operation.
- **Simple feed ranking:** Sentiment signals from 7 known types feed directly into the News Feed ranking algorithm.
- **Consistent UI:** The reaction bar under every post looks uniform and is easy to render.

---

## 6. Comparison with Other Platforms

### Reaction Model Comparison

| Platform | # of Types | Model | Extensibility | Custom Reactions |
|----------|-----------|-------|---------------|-----------------|
| **Facebook** | 7 (fixed, curated) | Integer type ID + registry | New types via registry INSERT + client update | No |
| **Slack** | Unlimited (any emoji) | String emoji code | Maximally extensible; any Unicode or custom emoji | Yes (workspace custom emoji) |
| **LinkedIn** | 6 (fixed, curated) | Similar to Facebook | Rare additions | No |
| **Twitter/X** | 1 (heart/like) | Boolean (liked or not) | None needed | No |
| **Discord** | Unlimited (any emoji) | String emoji code | Same as Slack | Yes (server custom emoji) |
| **iMessage** | 6 (fixed, curated) | Fixed set (Tapback) | Not extensible | No |

### Data Model Comparison

| Platform | Storage | Schema | Example Record |
|----------|---------|--------|----------------|
| **Facebook** | `reaction_type TINYINT` | Fixed counters per entity | `(user_id, entity_id, 2, timestamp)` where 2 = love |
| **Slack** | `emoji_code VARCHAR` | Group-by aggregation on read | `(user_id, message_id, ':rocket:', timestamp)` |
| **LinkedIn** | `reaction_type TINYINT` | Fixed counters per entity | `(user_id, entity_id, 4, timestamp)` where 4 = insightful |
| **Twitter/X** | `liked BOOLEAN` (implicit) | Single counter per tweet | `(user_id, tweet_id, timestamp)` |
| **Discord** | `emoji_code VARCHAR` | Group-by aggregation on read | `(user_id, message_id, ':custom_emoji_id:', timestamp)` |
| **iMessage** | `tapback_type TINYINT` | Fixed set, device-local | `(tapback_type, associated_message_guid)` |

### Extensibility vs. Complexity Tradeoff

```
Extensibility
     ^
     |  Discord  Slack
     |    *        *
     |
     |
     |  Facebook  LinkedIn
     |    *          *
     |
     |                    iMessage
     |                      *
     |              Twitter/X
     |                *
     +--------------------------> Simplicity
```

**Key insight:** Facebook sits in the sweet spot. It has enough reaction types to capture the range of human emotional responses to content (positive, funny, surprising, sad, angry, caring) without the complexity of unbounded emoji reactions. The curated set allows for predictable infrastructure (fixed counters, compact cache entries, tractable sentiment analysis) while still being extensible through the type registry when a genuinely new universal emotion needs representation.

---

## 7. Design Decision Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Number of reaction types | Small, curated set (7) | Predictable aggregation, compact caching, universal cross-cultural meaning |
| Storage format | TINYINT type ID | 1 byte per reaction; supports up to 255 types which is more than sufficient |
| Type metadata | Registry table | Decouples type definitions from schema; supports temporary reactions natively |
| Client compatibility | Fallback labels + periodic registry sync | Old clients never break when new types are added |
| Temporary reactions | `available_from`/`available_to` in registry | Enables event-tied reactions without data deletion |
| Aggregation | Pre-computed fixed counters per entity | O(1) read for reaction counts; trivially cacheable |
