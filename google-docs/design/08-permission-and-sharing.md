# 08 - Permission and Sharing Model

## Overview

Google Docs uses a fine-grained, real-time permission system that governs who can do what to a document at every moment. Permissions are checked not just at document open, but on **every single operation** flowing through the collaboration pipeline. This is one of the most interview-relevant subsystems because it intersects with real-time collaboration, consistency, caching, and security.

---

## 1. Permission Levels

Google Docs defines four hierarchical permission levels. Each level inherits all capabilities of the levels below it.

```
Owner > Editor > Commenter > Viewer
```

### Capabilities Matrix

| Capability                        | Owner | Editor | Commenter | Viewer |
|-----------------------------------|:-----:|:------:|:---------:|:------:|
| Read document content             |  Y    |   Y    |     Y     |   Y    |
| Download / print / copy           |  Y    |   Y    |     Y     | Config |
| Add comments                      |  Y    |   Y    |     Y     |   N    |
| Add suggestions (suggest mode)    |  Y    |   Y    |     Y     |   N    |
| Resolve / delete own comments     |  Y    |   Y    |     Y     |   N    |
| Resolve / delete others' comments |  Y    |   Y    |     N     |   N    |
| Edit document content directly    |  Y    |   Y    |     N     |   N    |
| Change formatting                 |  Y    |   Y    |     N     |   N    |
| Accept / reject suggestions       |  Y    |   Y    |     N     |   N    |
| Share with others                 |  Y    | Config |     N     |   N    |
| Change sharing settings           |  Y    |   N    |     N     |   N    |
| Transfer ownership                |  Y    |   N    |     N     |   N    |
| Delete document permanently       |  Y    |   N    |     N     |   N    |
| Move to trash                     |  Y    |   N    |     N     |   N    |
| Set download/print/copy policy    |  Y    |   N    |     N     |   N    |

> **Config** = Owner can configure whether editors can re-share and whether viewers can download/print/copy.

### Why Four Levels (Not Two)?

A simpler system (Viewer/Editor) forces a choice: either commenters can edit, or they cannot comment. Google's four-level hierarchy cleanly separates **feedback** (Commenter) from **authorship** (Editor) from **governance** (Owner). This matters in enterprise workflows where legal reviewers should annotate but never modify.

---

## 2. Sharing Mechanisms

### 2.1 Direct Sharing (Per-User)

The most granular method. The owner (or editor, if permitted) shares with specific email addresses.

```
┌─────────────────────────────────────────┐
│         Direct Sharing Request          │
├─────────────────────────────────────────┤
│  doc_id:       "doc_abc123"             │
│  grantor:      "alice@company.com"      │
│  grantee:      "bob@company.com"        │
│  permission:   EDITOR                   │
│  notify:       true                     │
│  message:      "Please review Ch. 3"   │
│  expiration:   2025-12-31T00:00:00Z     │
│                (optional)               │
└─────────────────────────────────────────┘
```

**Key properties:**
- Each user gets an individual ACL entry
- Permissions can differ per user on the same document
- Optional expiration dates for temporary access
- Notification email sent via async job (not blocking the share operation)

### 2.2 Link Sharing

Instead of enumerating users, the owner configures a link-level policy.

```
┌───────────────────────────────────────────────┐
│            Link Sharing Settings              │
├───────────────────────────────────────────────┤
│  Scope:                                       │
│    [ ] Restricted (only explicitly shared)    │
│    [x] Anyone in "company.com"                │
│    [ ] Anyone with the link                   │
│                                               │
│  Access Level:                                │
│    ( ) Viewer                                 │
│    (x) Commenter                              │
│    ( ) Editor                                 │
│                                               │
│  Link: https://docs.google.com/d/abc123/edit  │
└───────────────────────────────────────────────┘
```

**Three scopes:**

| Scope                     | Who can access                                    | Typical use case                       |
|---------------------------|---------------------------------------------------|----------------------------------------|
| Restricted                | Only users with explicit ACL entry                | Confidential docs                      |
| Anyone in organization    | Any authenticated user in the Google Workspace org | Internal company wiki pages            |
| Anyone with the link      | Any person with the URL (no auth needed for view) | Public documentation, blog drafts      |

**Important nuance:** A user's effective permission is the **maximum** of their direct ACL entry and the link-level permission. If the link grants Viewer access but Alice has a direct Editor entry, Alice is an Editor.

```
effective_permission(user, doc) = MAX(
    direct_acl_permission(user, doc),
    link_permission(doc)          -- if user qualifies for link scope
)
```

### 2.3 Domain-Level Sharing (Google Workspace)

Google Workspace admins can set organization-wide policies:

- **Default link sharing scope**: e.g., "Anyone in company.com" is the default for new docs
- **External sharing restrictions**: block sharing outside the organization entirely
- **Allowlisted external domains**: permit sharing with specific partner organizations
- **DLP (Data Loss Prevention) rules**: automatically restrict documents containing sensitive content

```
Organization Policy (Workspace Admin)
├── Allow external sharing?          → Yes / No
├── Default link sharing scope       → "Anyone in company.com" / Restricted
├── Allowed external domains         → ["partner.com", "vendor.com"]
├── Allow viewers to download/print? → Yes / No
└── DLP rules                        → [regex patterns → auto-restrict]
```

---

## 3. Permission Checking Architecture

### 3.1 When Permissions Are Checked

Permissions are validated at **two critical points**:

1. **WebSocket connection establishment**: Before the server accepts a collaboration session, it verifies the user's permission level. This determines the initial mode (edit vs. read-only).

2. **Every incoming operation**: Each OT operation arriving at the server includes the user's identity. The server checks permission before applying it.

```
Client sends operation
        │
        v
┌──────────────────┐
│  WebSocket GW    │──── Is connection authenticated? ─── No ──> Reject
│                  │                │
└──────────────────┘               Yes
        │                          │
        v                          v
┌──────────────────┐    ┌─────────────────────┐
│   OT Server      │───>│  Permission Check    │
│                  │    │  (cached ACL lookup) │
│                  │    └─────────────────────┘
│                  │          │           │
│                  │        Allowed     Denied
│                  │          │           │
│                  │          v           v
│                  │     Transform &   Drop op,
│                  │     broadcast     notify client
└──────────────────┘
```

### 3.2 Why Speed Matters

Every operation goes through permission checking. At scale:
- A popular document might receive **hundreds of operations per second**
- Permission check must complete in **< 1ms** to avoid becoming a bottleneck
- This rules out a database query per check

### 3.3 Cached ACL Design

```
┌───────────────────────────────────────────────────┐
│              ACL Cache (per OT server)            │
├───────────────────────────────────────────────────┤
│                                                   │
│  Key: (doc_id, user_email)                        │
│  Value: {                                         │
│      permission_level: EDITOR,                    │
│      cached_at: 1700000000,                       │
│      ttl: 60s                                     │
│  }                                                │
│                                                   │
│  Eviction: LRU + TTL                              │
│  Invalidation: push-based from permission service │
│                                                   │
│  Lookup path:                                     │
│    1. In-memory hash map (O(1))     → ~0.01ms     │
│    2. Cache miss → Permission DB    → ~5-10ms     │
│    3. Refresh on TTL expiry                       │
│                                                   │
└───────────────────────────────────────────────────┘
```

**Cache invalidation strategy:**

When a permission change occurs (e.g., owner downgrades editor to viewer), the Permission Service:
1. Writes the new ACL to the database (Spanner)
2. Publishes an invalidation event to a Pub/Sub topic
3. All OT servers subscribed to that document receive the event
4. They invalidate their local cache entry
5. Next permission check fetches the fresh value

This is **push-based invalidation** — critical because TTL alone could allow a revoked editor to send operations for up to 60 seconds after revocation.

---

## 4. Real-Time Permission Changes

This is the hard part. What happens when an owner downgrades an editor to a viewer **while that editor is actively typing**?

### 4.1 Permission Downgrade Flow

```
Timeline:

t=0   Bob is editing document (permission = EDITOR)
      Bob has 3 pending operations in flight (not yet ACKed by server)

t=1   Alice (owner) changes Bob's permission to VIEWER
      │
      ├── 1. Spanner write: UPDATE acl SET level='VIEWER'
      │       WHERE doc_id='abc' AND user='bob@...'
      │
      ├── 2. Pub/Sub event: {doc_id, user, new_level: VIEWER}
      │
      ├── 3. OT server receives event
      │       ├── Invalidates cached ACL for Bob
      │       ├── Rejects Bob's 3 pending operations
      │       └── Sends WebSocket message to Bob's client:
      │           {type: "permission_changed", new_level: "VIEWER"}
      │
      └── 4. Bob's client receives notification
              ├── Switches UI to read-only mode
              ├── Discards pending (unACKed) operations
              ├── Disables text cursor and input
              └── Shows toast: "Your access has been changed to Viewer"

t=2   Bob can still see real-time updates from other editors
      Bob cannot send any more operations
```

### 4.2 Edge Cases

| Scenario | Handling |
|----------|----------|
| Op arrives at server between DB write and cache invalidation | Short race window. Push-based invalidation minimizes this to milliseconds. Acceptable risk. |
| User has doc open in multiple tabs | All WebSocket connections for that user receive the downgrade notification |
| Upgrade (viewer to editor) while viewing | Client receives upgrade notification, enables editing UI, no data loss risk |
| Owner removes themselves | Prevented by the API — owner cannot remove their own access without transferring ownership first |
| Last owner leaves organization | Workspace admin inherits ownership via organization policy |

### 4.3 Conflict: Permission Change vs. In-Flight Operations

```
Server receives Bob's operation at t=1.001
Permission change was written at t=1.000

Decision tree:
  IF permission_change.timestamp < operation.server_receive_time
  AND cache is already invalidated
  THEN reject operation

  IF cache is not yet invalidated (race condition)
  THEN operation may be accepted
  → This is a known, bounded inconsistency window (~10-50ms)
  → Acceptable because:
     1. The operation was legitimately authored when Bob was still an editor
     2. The window is extremely small
     3. The alternative (distributed lock) would add unacceptable latency
```

---

## 5. Storage Model

### 5.1 ACL Table in Spanner

Google uses **Spanner** for ACL storage because it provides:
- **Strong consistency** across regions (critical — stale permissions = security bug)
- **Low-latency reads** (single-digit ms from nearest replica)
- **Atomic transactions** (change multiple ACL entries atomically)

```sql
CREATE TABLE document_acl (
    doc_id          STRING(64)   NOT NULL,
    user_email      STRING(320)  NOT NULL,
    permission_level STRING(16)  NOT NULL,  -- OWNER, EDITOR, COMMENTER, VIEWER
    granted_by      STRING(320)  NOT NULL,
    granted_at      TIMESTAMP    NOT NULL,
    expires_at      TIMESTAMP,              -- NULL = no expiration

    -- Interleave with parent document table for locality
) PRIMARY KEY (doc_id, user_email),
  INTERLEAVE IN PARENT documents ON DELETE CASCADE;
```

```sql
CREATE TABLE document_link_sharing (
    doc_id          STRING(64)  NOT NULL,
    scope           STRING(32)  NOT NULL,  -- RESTRICTED, ORGANIZATION, ANYONE
    link_permission STRING(16)  NOT NULL,  -- VIEWER, COMMENTER, EDITOR
    organization_id STRING(64),            -- NULL if scope = ANYONE
    updated_at      TIMESTAMP   NOT NULL,
    updated_by      STRING(320) NOT NULL,

) PRIMARY KEY (doc_id);
```

### 5.2 Permission Resolution Query

```sql
-- Resolve effective permission for a user on a document
SELECT
    GREATEST(
        COALESCE(acl.permission_level, 'NONE'),
        CASE
            WHEN ls.scope = 'ANYONE' THEN ls.link_permission
            WHEN ls.scope = 'ORGANIZATION'
                 AND @user_org = ls.organization_id THEN ls.link_permission
            ELSE 'NONE'
        END
    ) AS effective_permission
FROM document_link_sharing ls
LEFT JOIN document_acl acl
    ON ls.doc_id = acl.doc_id AND acl.user_email = @user_email
WHERE ls.doc_id = @doc_id
    AND (acl.expires_at IS NULL OR acl.expires_at > CURRENT_TIMESTAMP());
```

> In practice, this is not a raw SQL query at every check. It runs once to populate the cache, then the cache is used until invalidated.

### 5.3 Why Spanner Over Other Databases?

| Requirement | Spanner | DynamoDB | PostgreSQL |
|-------------|---------|----------|------------|
| Strong global consistency | Yes (TrueTime) | Eventually consistent (or strongly consistent per-region) | Single-region only |
| Multi-region replication | Built-in | Global tables (eventually consistent) | Manual replication |
| Low-latency reads | Yes (nearest replica) | Yes | Yes (single region) |
| Atomic cross-row transactions | Yes | Limited (single partition) | Yes (single region) |
| Google-internal integration | Native | N/A | Would need proxy |

---

## 6. Comparison with Other Systems

### 6.1 Dropbox Paper

| Aspect | Google Docs | Dropbox Paper |
|--------|-------------|---------------|
| Permission levels | 4 (Owner/Editor/Commenter/Viewer) | 2 (Editor/Viewer) + folder-level |
| Sharing granularity | Per-document | Per-folder inheritance + per-doc override |
| Link sharing | 3 scopes | 2 scopes (anyone / team) |
| Real-time permission enforcement | Per-operation | Per-session |
| Enterprise DLP | Deep integration | Limited |

Dropbox's simpler model works because Paper is not their core product — file sync is. Their permission model is **folder-centric** (permissions flow down from shared folders), which is simpler to reason about but less flexible for individual documents.

### 6.2 Microsoft 365 (Word Online)

| Aspect | Google Docs | Microsoft 365 |
|--------|-------------|---------------|
| Permission levels | 4 | 4+ (Reader/Contributor/Editor/Full Control in SharePoint) |
| Permission source | Per-document ACL | SharePoint site/library permissions + per-doc |
| Co-authoring permission | Implicit (if editor, you can co-author) | Explicit co-author state tracked |
| Conflict with permissions | Reject operations immediately | Lock-based sections may block |
| Admin control | Workspace admin console | SharePoint + Azure AD + Compliance Center |

Microsoft's model is **more complex** because it inherits SharePoint's permission system, which supports sites, subsites, libraries, folders, and items — each with its own ACL. This power comes at the cost of admin complexity and occasional confusion about why someone can or cannot access a document.

### 6.3 Notion

| Aspect | Google Docs | Notion |
|--------|-------------|--------|
| Permission model | Per-document | Per-page + workspace hierarchy |
| Permission inheritance | None (each doc independent) | Pages inherit from parent pages |
| Guest access | Link sharing + direct share | Explicit guest invitations |
| Real-time enforcement | Per-operation | Per-session (block-based) |

Notion's hierarchical permission model (workspace > teamspace > page > sub-page) is more natural for wikis but adds complexity in permission resolution — you must walk up the tree to determine effective permissions.

---

## 7. Interview Talking Points

**If asked "How do you handle permission checking at scale?":**
> We cache ACL entries in memory on the OT server. Every operation is checked against this cache in O(1). Cache is invalidated via push-based Pub/Sub when permissions change, so the staleness window is bounded to milliseconds, not seconds.

**If asked "What if someone's permission is revoked while they're editing?":**
> The permission change writes to Spanner, then publishes an invalidation event. The OT server drops the user's pending operations, sends a WebSocket notification, and the client switches to read-only mode. There's a bounded race window of ~10-50ms where an operation might slip through — this is acceptable because the operation was authored when the user was still authorized.

**If asked "Why not use a distributed lock for permission changes?":**
> A distributed lock would add 50-100ms of latency to every operation for permission checking. At hundreds of ops/sec per document, this would destroy the real-time editing experience. The bounded inconsistency window from push-based cache invalidation is a deliberate trade-off: security is eventually enforced (within milliseconds), while latency remains sub-millisecond for the common path.

**If asked "How does this scale to billions of documents?":**
> ACL data is stored in Spanner, interleaved with the document table for locality. Most permission checks never hit the database — they're served from the OT server's in-memory cache. Only active documents have cached permissions, so the memory footprint scales with concurrent users, not total documents. For a system with 10M concurrent editing sessions, cache memory is roughly 10M * ~200 bytes = ~2GB — negligible.
