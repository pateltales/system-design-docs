# Deep Dive: Sharing & Permissions

> **Context:** Sharing transforms Dropbox from "personal backup" into a "collaboration platform." It's also where the most complex metadata and consistency challenges arise — shared namespaces, ACL inheritance, and notification fan-out all converge here.

---

## Opening

**Interviewer:**

Walk me through how sharing works. What happens when Alice shares a folder with Bob?

**Candidate:**

> Sharing a folder is one of the most complex operations in Dropbox because it touches every layer of the system:
>
> 1. **Metadata**: Create a shared namespace, mount it in both users' trees
> 2. **Permissions**: Set up ACL (who can view/edit)
> 3. **Sync**: Both users' clients need to discover and sync the shared content
> 4. **Notifications**: Both users need real-time change propagation
> 5. **Conflict resolution**: More users = more potential conflicts

---

## 1. Sharing Models

**Candidate:**

> Dropbox supports three sharing mechanisms:
>
> ### 1.1 Shared Folders (Full Collaboration)
>
> ```
> ┌────────────────────────────────────────────────────────────┐
> │  SHARED FOLDER: /SharedProject/ (namespace 2001)           │
> │                                                            │
> │  Members:                                                  │
> │  ├── Alice (owner)  — mounted at /Alice/SharedProject/     │
> │  ├── Bob (editor)   — mounted at /Bob/Work/SharedProject/  │
> │  └── Charlie (viewer) — mounted at /Charlie/Shared/Project/│
> │                                                            │
> │  Contents (single source of truth):                        │
> │  ├── design.psd                                            │
> │  ├── spec.docx                                             │
> │  └── assets/                                               │
> │      ├── logo.png                                          │
> │      └── banner.jpg                                        │
> │                                                            │
> │  When Bob edits spec.docx:                                 │
> │  1. Change recorded in namespace 2001's change journal     │
> │  2. Alice and Charlie's longpolls wake up                  │
> │  3. Both clients download changed blocks                   │
> │  4. File appears updated on all three devices              │
> └────────────────────────────────────────────────────────────┘
> ```
>
> **Key properties:**
> - The folder appears in each member's file tree as if it were their own
> - All members see the same content, synced in real-time
> - Changes by any editor propagate to all members
> - Viewers can download/read but not modify
>
> ### 1.2 Shared Links (Quick Sharing)
>
> ```
> Link: https://www.dropbox.com/s/abc123/report.pdf?dl=0
>
> Properties:
>   - Visibility: public | team_only | password_protected
>   - Expiry: optional date
>   - Access: view-only (no editing via link)
>   - No account required to view
>   - No sync (viewers see a web preview, not a local file)
>
> Use case: Share a file with someone who doesn't have Dropbox
>           or doesn't need ongoing collaboration.
> ```
>
> ### 1.3 Direct Member Sharing (per-folder)
>
> Unlike Google Drive, Dropbox shares at the **folder level**, not the file level. To share a single file, you either:
> - Create a shared link (read-only, no collaboration)
> - Put the file in a shared folder (full collaboration)
>
> **This is a deliberate simplification**: per-file sharing creates a permission explosion (every file has its own ACL). Per-folder sharing keeps permissions manageable — one ACL per shared folder, inherited by all contents.

---

## 2. Shared Namespaces — The Core Mechanism

**Interviewer:**

How does a shared folder technically work? Walk me through the namespace creation.

**Candidate:**

> When Alice shares `/Projects/Q1` with Bob:
>
> ```
> BEFORE sharing:
>
> Alice's namespace (NS 1001):
>   /Projects/
>   /Projects/Q1/           ← regular folder, Alice-only
>   /Projects/Q1/spec.docx
>   /Projects/Q1/design.psd
>
> SHARING OPERATION:
>
> Step 1: Create shared namespace
>   New namespace NS 2001 (type: shared_folder)
>   Move all files from /Projects/Q1/ into NS 2001
>
> Step 2: Mount into Alice's tree
>   namespace_mounts: (alice, NS 2001, "/Projects/Q1/", owner)
>
> Step 3: Add Bob as member
>   namespace_mounts: (bob, NS 2001, "/SharedProject/", editor)
>   (Bob chooses where to mount it in HIS tree)
>
> Step 4: Initialize Bob's sync
>   Bob's client discovers new namespace via longpoll
>   Downloads all files in NS 2001
>
> AFTER sharing:
>
> Alice's tree:                    Bob's tree:
> /Projects/                       /SharedProject/
> /Projects/Q1/ → NS 2001         /SharedProject/ → NS 2001
>   spec.docx                        spec.docx
>   design.psd                       design.psd
>
> Both point to the SAME namespace. One copy of the data.
> ```
>
> ### What a shared namespace owns:
>
> | Property | Description |
> |----------|-------------|
> | **Change journal** | Own cursor, own sequence of mutations. When Bob edits a file, cursor advances in NS 2001. |
> | **ACL** | Owner (Alice), editors (Bob), viewers. Per-namespace, not per-file. |
> | **MySQL shard** | All files in NS 2001 are on the same MySQL shard. Enables ACID operations within the folder. |
> | **Mount points** | Each member has a mount entry: `(user_id, namespace_id, mount_path, access_level)` |
> | **Block references** | Files in the namespace reference blocks in Magic Pocket. Reference counts include ALL files across ALL namespaces. |

---

## 3. ACL (Access Control List) Model

**Candidate:**

> ### Roles:
>
> | Role | Read | Write | Delete | Share | Transfer Ownership |
> |------|------|-------|--------|-------|--------------------|
> | **Viewer** | ✅ | ❌ | ❌ | ❌ | ❌ |
> | **Editor** | ✅ | ✅ | ✅ | Depends on policy | ❌ |
> | **Owner** | ✅ | ✅ | ✅ | ✅ | ✅ |
>
> ### Inheritance:
>
> Permissions cascade down the folder hierarchy. If Alice shares `/Project/` with Bob as editor, Bob can edit all files and subfolders:
>
> ```
> /Project/ (Bob: editor)
>   ├── spec.docx          ← Bob can edit ✅ (inherited)
>   ├── design/
>   │   ├── mockup.psd     ← Bob can edit ✅ (inherited from /Project/)
>   │   └── assets/
>   │       └── logo.png   ← Bob can edit ✅ (inherited from /Project/)
>   └── confidential/      ← Could be a NESTED shared folder with different ACL
>       └── budget.xlsx    ← If nested share excludes Bob, he can't access this
> ```
>
> **Nested shared folders**: A subfolder can have additional sharing restrictions. `/Project/confidential/` could be shared with only Alice and the CFO, excluding Bob. This creates a **namespace boundary** — `confidential/` becomes its own namespace with its own ACL.
>
> ### Permission check flow:
>
> ```
> Request: Bob wants to edit /Project/design/mockup.psd
>
> 1. Resolve path to namespace: /Project/ → NS 2001
> 2. Look up Bob's access: namespace_mounts WHERE user=bob AND ns=2001
>    → access_level = "editor"
> 3. Check file path: /design/mockup.psd is within NS 2001
> 4. No nested shared folder overrides this path
> 5. Result: ALLOW (editor can write)
>
> This check happens on EVERY API call.
> With 95% cache hit rate on namespace/ACL data, it's fast.
> ```

---

## 4. Team & Organization Features

**Candidate:**

> Dropbox Business adds enterprise-grade sharing controls:
>
> ### Team Folders
> Admin-managed folders owned by the organization (not any individual). When an employee leaves, their personal Dropbox is removed but team folders persist.
>
> ### Admin Console
> ```
> Admin capabilities:
> - View all team members and their storage usage
> - Set per-user storage quotas
> - View sharing activity (who shared what with whom)
> - Manage team-wide sharing policies:
>   - "Members can only share with other team members" (no external sharing)
>   - "Shared links require password and expiry"
>   - "Disable downloads from shared links"
> ```
>
> ### Data Loss Prevention (DLP)
> Scan shared files for sensitive data (credit card numbers, SSNs, confidential keywords). Alert admin or block sharing when sensitive data is detected.
>
> ### Device Approval
> Restrict which devices can sync company data. Only approved devices (managed laptops) can install the Dropbox desktop client.
>
> ### Remote Wipe
> If an employee's laptop is lost or stolen, admin can remotely wipe the Dropbox folder from that device on next connection. The data remains in the cloud, just removed from the local device.
>
> ### Audit Log
> ```
> Audit events tracked:
> - File created, edited, deleted, moved, renamed
> - File shared (with whom, access level)
> - Shared link created/modified/revoked
> - Member added/removed from shared folder
> - Login events (device, IP, location)
> - Admin actions (quota changes, policy changes)
>
> Retention: 6 months (Business), 1+ year (Enterprise/compliance tiers)
> ```

---

## 5. Sharing Scale Challenges

**Interviewer:**

What happens when a shared folder has 1000 members?

**Candidate:**

> Large shared folders create several scaling challenges:
>
> ### Notification fan-out
> ```
> When a file changes in a 1000-member shared folder:
>
> 1. Server appends to namespace change journal (1 write)
> 2. Look up all 1000 members of this namespace
> 3. For each member with an active longpoll → respond (up to 1000 responses)
> 4. For members without active polls → they pick up on next poll
>
> This is O(N) fan-out per change. With frequent edits, this becomes:
>   10 edits/minute × 1000 members = 10,000 notifications/minute
>
> Mitigation:
> - Batch/coalesce rapid changes (5 edits in 10 sec → 1 notification)
> - Longpoll response is lightweight ("changes exist", not the changes themselves)
> - Each notification is < 100 bytes — 10K notifications = ~1 MB, manageable
> ```
>
> ### Conflict probability
> ```
> With 1000 collaborators, the probability of two people editing
> the same file simultaneously increases dramatically.
>
> If each member edits a file once per day:
>   Average gap between edits = 86,400 sec / 1000 = 86 seconds
>   If sync takes ~10 seconds, overlap probability per edit ≈ 10/86 ≈ 11.6%
>
> Compare to 2 collaborators:
>   Average gap = 43,200 seconds
>   Overlap probability ≈ 10/43200 ≈ 0.02%
>
> Large shared folders generate MANY more conflicted copies.
> This is why real-time collaborative editing (Google Docs OT)
> is a better model for large teams editing the same documents.
> ```
>
> ### Metadata hot shard
> All 1000 members' sync operations hit the same MySQL shard (NS is on one shard). Frequent changes create a hot shard.
>
> Mitigation:
> - Dedicated hardware for hot shards
> - Read replicas for the namespace's metadata
> - Cache aggressively (ACL data, member list, directory listings)

---

## 6. Revocation

**Candidate:**

> When Alice removes Bob from a shared folder, several things happen:
>
> ```
> Revocation flow:
>
> 1. Server: Remove Bob's mount entry for NS 2001
> 2. Server: Update ACL (remove Bob from member list)
> 3. Server: Notify Bob's client (via longpoll or next sync)
> 4. Bob's client: Receive "namespace removed" event
> 5. Bob's client: Delete local copy of shared folder contents
> 6. Bob's client: Remove namespace from sync tracking
>
> Security considerations:
> - Bob may have already downloaded files locally → local copies persist
>   (Dropbox can't delete files Bob copied elsewhere)
> - Enterprise: Remote wipe can force-delete local Dropbox files
> - Shared links Bob created within the folder should be revoked
> - Bob's offline changes (queued but not synced) are discarded
> ```

---

## Contrast: Dropbox vs Google Drive vs S3

| Aspect | Dropbox | Google Drive | S3 |
|--------|---------|-------------|-----|
| **Sharing unit** | Folder (shared namespace) | File or folder | Bucket (IAM + bucket ACL) |
| **Roles** | Owner, Editor, Viewer | Owner, Editor, Commenter, Viewer | IAM policies (programmatic) |
| **Per-file sharing** | No (folder-level only) | Yes | Yes (object-level policies) |
| **Real-time collab** | No (conflicted copies) | Yes (Google Docs OT/CRDT) | No |
| **Shared links** | Yes (public, password, expiry) | Yes (similar) | Pre-signed URLs (time-limited) |
| **Permission model** | Hierarchical (folder inheritance) | Per-resource (files can be in multiple folders) | IAM (policy-based, programmatic) |
| **Domain sharing** | Team/org restrictions | Domain-wide ("anyone in company.com") | VPC endpoints, IAM roles |
| **Namespace** | Shared namespace (single source of truth, mounted in multiple trees) | File in multiple parents (Google Drive folders are labels, not true hierarchy) | N/A (flat key-value) |
| **Audit** | Business/Enterprise plans | Google Workspace audit log | CloudTrail |
>
> **Key difference**: Dropbox shared folders are true **shared namespaces** — one copy of data, multiple mount points. Google Drive files can exist in multiple "folders" simultaneously (because Google Drive folders are more like labels/tags). S3 doesn't have end-user sharing — it's all IAM policies for programmatic access.

---

## L5 vs L6 vs L7 — Sharing Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Model** | "Share files with a link" | Designs shared folders with namespaces, explains mount points, ACL roles | Explains namespace isolation (shard co-location, per-namespace cursor, ACL boundary), designs nested shared folder semantics |
| **Permissions** | "Add viewers and editors" | Designs role-based ACL with inheritance, permission check on every API call | Discusses enterprise features (DLP, device approval, remote wipe, audit), designs revocation flow including edge cases (offline changes, local copies) |
| **Scale** | "Share with many users" | Identifies notification fan-out as O(N), discusses batching/coalescing | Calculates conflict probability vs team size, explains why Dropbox's model breaks down at large scale (favors Google Docs OT for large teams) |
| **Contrast** | None | Contrasts with Google Drive (per-file sharing, commenter role) | Three-way comparison: Dropbox (namespace-centric), Google Drive (file-centric with labels), S3 (IAM policies) — explains how each reflects product philosophy |

---

> **Summary:** Sharing in Dropbox is built on the **shared namespace** abstraction — a shared folder becomes its own namespace with its own change journal, ACL, and MySQL shard. This namespace is "mounted" into each member's file tree, appearing as a native folder. Permissions are per-folder (not per-file) for simplicity, with hierarchical inheritance. Enterprise features (DLP, audit, remote wipe) layer security controls on top. The model works well for small-to-medium teams but creates scaling challenges (notification fan-out, conflict probability, hot shards) for very large shared folders — which is why Google's approach of real-time collaborative editing is better suited for large teams editing the same documents.
