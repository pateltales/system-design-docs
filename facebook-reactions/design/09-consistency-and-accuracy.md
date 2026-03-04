# Consistency, Accuracy, and Edge Cases

---

## 1. Consistency Model

Facebook Reactions operates under a **tiered consistency model** — different pieces of data get different guarantees depending on how critical they are to user experience.

### Individual Reaction Records — Strong Consistency

Each reaction is stored with a unique constraint on `(userId, entityId)`. This means:

- You can always definitively answer: **"Did I react to this post?"**
- There is exactly zero or one reaction per user per entity at any time.
- The unique constraint is enforced at the MySQL (persistent storage) layer, so even under concurrent writes, duplicates are impossible.

```
UNIQUE INDEX idx_user_entity (user_id, entity_id)
```

This is the strongest guarantee in the system. It never drifts.

### Reaction Counts — Eventually Consistent

Pre-aggregated counts (e.g., "1,203 likes, 47 loves") are **eventually consistent** with a convergence window of roughly **5-10 seconds**.

This is an acceptable tradeoff:

- A user sees the count go from 1,200 to 1,201 within seconds, not instantly.
- Nobody notices or cares about a few seconds of delay on a count.
- The alternative — strongly consistent counts via distributed transactions — would be orders of magnitude more expensive for negligible UX benefit.

### Read-Your-Own-Writes — Critical

When a user taps "love", they **must immediately see their own reaction reflected**. Seeing stale state for your own action feels broken.

This is achieved via two complementary mechanisms:

1. **Optimistic client-side update**: The mobile/web client immediately renders the reaction before the server confirms. The UI shows the heart icon and increments the count locally. If the server later rejects (rare), the client rolls back.

2. **TAO's read-your-own-writes guarantee**: Within the same region, TAO ensures that any read after a write reflects that write. The reacting user's subsequent reads are routed to the same region that handled their write (sticky routing), so they always see their own update even at the server level.

---

## 2. Count Drift Problem

Over time, pre-aggregated counts can **drift** from the true count derived from individual reaction records. This is an inherent risk of maintaining denormalized counters.

### How Drift Happens

| Scenario | What Goes Wrong |
|---|---|
| **Missed increment** | Write to the count table failed, but the reaction record was successfully inserted. Count is 1 behind reality. |
| **Double decrement** | A retry of an "unreact" operation causes the count to be decremented twice. Count is 1 below reality. |
| **Failed type change** | Crash occurs between decrementing the old type count and incrementing the new type count. One type is too low, the other is correct. Net total is off by 1. |
| **Partial transaction failure** | Any crash or timeout between the reaction record write and the count update leaves the two out of sync. |

None of these are bugs per se — they are the inevitable consequence of not using distributed transactions (which would be too expensive at Facebook scale).

### Solutions

#### Periodic Reconciliation

A background job runs `SELECT COUNT(*) ... GROUP BY reactionType WHERE entityId = ?` on the actual reaction records and overwrites the pre-aggregated count with the true value.

- **Hot entities** (posts with recent activity): reconciled **hourly**.
- **Cold entities** (posts with no recent activity): reconciled **daily**.
- This is the primary defense against drift. It bounds the error window.

```
-- Reconciliation query for a single entity
SELECT reaction_type, COUNT(*) AS true_count
FROM reactions
WHERE entity_id = ?
GROUP BY reaction_type;

-- Then overwrite the pre-aggregated counts
UPDATE reaction_counts
SET count = <true_count>
WHERE entity_id = ? AND reaction_type = ?;
```

#### Audit Events

Every increment and decrement is logged to a durable event stream (Kafka or equivalent). These events can be replayed to verify that the count table matches the sum of all operations.

If the replayed total diverges from the stored count, a reconciliation is triggered for that entity.

#### Checksum Approach

Maintain a rolling checksum of reaction operations per entity. Both the count table and the event log compute this checksum independently. If the checksums diverge, it means an operation was lost or double-applied, and reconciliation is triggered immediately rather than waiting for the next scheduled run.

#### Probabilistic Verification

Random sampling as a safety net — periodically pick random entities and verify their counts against the source-of-truth reaction records. If drift exceeds a configurable threshold (e.g., >1% of sampled entities are off), alert the oncall team. This catches systematic issues that entity-level reconciliation might miss.

---

## 3. Race Conditions

### Double React

User taps "like" twice quickly. Two write requests arrive at the server.

- **What prevents double counting**: The unique constraint on `(userId, entityId)` means the operation uses **upsert semantics** (`INSERT ... ON DUPLICATE KEY UPDATE`). The second write is a no-op (or overwrites with the same value). The count is incremented exactly once.

### React + Unreact

User likes a post, then unlikes it within milliseconds. Both operations are in flight simultaneously.

- **Resolution**: Last write wins at the unique constraint level. If the "unreact" (delete) lands after the "react" (insert), the final state is no reaction — correct. If the "react" lands after the "unreact", the final state is reacted — the user sees a stale like and can tap unlike again. Both are acceptable outcomes.

### Concurrent Type Change

User changes from "like" to "love" while a separate request to "unreact" is in flight (e.g., from a different code path or a retry).

- **Risk**: The type-change writes "love" while the unreact deletes the record. Depending on ordering, the user could end up with no reaction (unreact wins) or a "love" reaction (type change wins).
- **Mitigation**: Use a **version/revision field** on the reaction record for optimistic locking. Each write includes the expected version. If the version has changed, the write is rejected and the client must re-read and retry.

```
UPDATE reactions
SET reaction_type = 'LOVE', version = version + 1
WHERE user_id = ? AND entity_id = ? AND version = <expected_version>;
-- If 0 rows affected → conflict detected, re-read and retry
```

### Multi-Device Conflict

User reacts on their phone and tablet simultaneously (e.g., "like" on phone, "love" on tablet).

- **Resolution**: Same upsert semantics — last writer wins. One device's reaction overwrites the other. Both devices eventually converge to the same state when they next fetch the post.
- This is a rare edge case and "last writer wins" is a perfectly acceptable resolution.

---

## 4. Deleted Entities

When a post is deleted, what happens to its millions of reactions?

### Option A: Cascade Delete

Delete all reaction records in the same transaction as the post deletion.

- **Problem**: For a viral post with millions of reactions, this locks the database for an unacceptable duration. A single `DELETE FROM reactions WHERE entity_id = ?` affecting 10 million rows would block other writes to that table.
- **Verdict**: Not viable at scale.

### Option B: Soft Delete (Facebook's Approach)

1. Mark the post as deleted (set a `deleted_at` timestamp or `is_deleted` flag).
2. Reaction records **remain in the database** but are no longer queryable — any query for reactions checks the post's deleted status first.
3. Pre-aggregated counts are **cleared immediately** (set to 0) so they don't appear in any UI.
4. A background **garbage collection job** asynchronously cleans up the orphaned reaction records hours or days later, in small batches to avoid DB pressure.

```
-- Immediate: clear counts
UPDATE reaction_counts SET count = 0 WHERE entity_id = ?;

-- Background GC (runs later, in batches)
DELETE FROM reactions WHERE entity_id = ? LIMIT 10000;
-- Repeat until no rows remain
```

This approach decouples the user-facing operation (post disappears instantly) from the storage cleanup (happens gradually in the background).

---

## 5. Blocked Users and Privacy

If Alice blocks Bob:

| Aspect | Behavior |
|---|---|
| **Can Bob react to Alice's post?** | Yes — blocking does not prevent the write. Bob's reaction is stored normally. |
| **Does Alice see Bob's reaction in "who reacted" list?** | No — Bob is filtered out at read time. |
| **Does Bob's reaction count toward the total?** | Yes — the count includes all reactions, regardless of block relationships. Alice sees "1,203 likes" which includes Bob's like. |
| **Does Alice get a notification about Bob's reaction?** | No — the notification pipeline checks Alice's block list and suppresses it. |

### Implementation

This is a **read-time filter**, not a write-time restriction.

```
-- When Alice views "who reacted":
SELECT r.user_id, r.reaction_type
FROM reactions r
WHERE r.entity_id = ?
  AND r.user_id NOT IN (
    SELECT blocked_user_id FROM blocks WHERE blocker_user_id = <alice_id>
  )
ORDER BY r.created_at DESC
LIMIT 50;
```

The block list is typically cached (it's small per user and read frequently), so this filter is cheap.

---

## 6. Deactivated and Deleted Users

### Deactivated Accounts

When a user deactivates their account (temporary):

- Their reaction records **remain in the database**.
- In "who reacted" lists, their name displays as **"Facebook User"** (anonymized).
- Counts are **unchanged** — their reactions still contribute to totals.
- If the user reactivates, everything returns to normal.

### Permanently Deleted Accounts

When a user permanently deletes their account:

- Reaction records are **removed** asynchronously via a data deletion pipeline.
- Pre-aggregated counts are **decremented** as each reaction is deleted.
- This happens in batches over hours/days — a user with thousands of reactions across thousands of posts generates thousands of count decrements.

### GDPR Compliance

Reaction data constitutes **personal data** under GDPR (it reveals that a specific person expressed a sentiment about specific content).

- Must be fully deletable within **30 days** of an account deletion request.
- The data deletion pipeline must track completion and provide auditability.
- This includes reaction records, associated audit events, cached reaction lists, and any analytics derived from the reaction.

---

## 7. Cross-Region Consistency

TAO uses **single-leader replication**: one region is designated as the primary for writes to a given entity.

### Replication Lag

Read replicas in other regions serve reads with approximately **1-5 seconds of replication lag** (async replication from MySQL primary to replicas).

**Example**: A post by a user in New York gets a reaction. A viewer in London might see the old count for a few seconds until the replication catches up.

### Sticky Routing for the Reacting User

For the user who just reacted, their subsequent reads are **routed to the region that handled their write**. This ensures read-your-own-writes consistency even in a globally distributed system.

```
User taps "love" → Write handled by US-EAST region
User refreshes feed → Read routed to US-EAST (sticky)
                    → Sees their own "love" immediately

Other user in EU → Read served by EU-WEST replica
                 → Might see old count for 1-5 seconds
```

### TAO's Lease-Based Invalidation

TAO uses **lease-based cache invalidation** to prevent thundering herd problems. When a cached value is invalidated:

- A lease (short-lived lock) is granted to one reader to go fetch the new value.
- All other readers either get the stale value or wait briefly.
- This prevents thousands of cache misses from simultaneously hitting the database when a popular entity's count changes.

---

## 8. Contrasts with Other Platforms

| Platform | Consistency Approach | Notable Difference |
|---|---|---|
| **YouTube** | Hid public dislike counts entirely (2021). Dislike data is still stored and counted internally, but the count is not displayed to viewers. This is a **product-level consistency decision** — accuracy exists but is intentionally not exposed. |  |
| **Reddit** | Uses **fuzzy vote counts** — intentionally adds noise to displayed scores. The true count is known internally, but the displayed number fluctuates. This reduces the precision requirement for caching (approximate counts are easier to cache aggressively). |  |
| **Instagram** | Runs on the **same TAO infrastructure** as Facebook. Similar eventual consistency model for counts, same read-your-own-writes guarantees, same reconciliation approach. Instagram's "hide like counts" experiment was also a product decision, not a technical one. |  |

### Key Takeaway

The consistency model is not purely a technical decision. It is shaped by product requirements:

- **Facebook** needs accurate, visible counts — so it invests in reconciliation infrastructure.
- **YouTube** decided accuracy doesn't matter for public dislikes — so it removed the display entirely.
- **Reddit** decided exact accuracy isn't worth the infrastructure cost — so it intentionally fuzzes numbers.

Each approach is valid for its context. The technical system should serve the product requirement, not the other way around.
