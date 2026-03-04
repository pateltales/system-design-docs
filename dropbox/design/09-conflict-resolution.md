# Deep Dive: Conflict Resolution

> **Context:** Conflicts are inevitable in a multi-device, multi-user sync system. How you handle them defines the user experience. Dropbox chose "conflicted copies" — a deliberately safe, simple approach that prioritizes data preservation over convenience.

---

## Opening

**Interviewer:**

Conflicts are the hardest UX problem in a sync system. Walk me through Dropbox's approach.

**Candidate:**

> The fundamental tension: **automatic sync means users don't coordinate edits.** Two people can edit the same file simultaneously without knowing it. The system must detect this and resolve it without losing data.
>
> Dropbox's philosophy: **data safety over user convenience.** An extra "conflicted copy" file is annoying. Losing someone's edits is catastrophic. Given the choice, always preserve both versions and let the user decide.

---

## 1. When Conflicts Occur

**Candidate:**

> ### Scenario 1: Multi-Device (Same User)
>
> ```
> Timeline:
>
> Alice's Laptop          Server          Alice's Phone
>   │                        │                │
>   │  file.txt (rev 5)      │  (rev 5)       │  file.txt (rev 5)
>   │                        │                │
>   │  ✈️ Laptop goes offline │                │
>   │  (airplane mode)       │                │
>   │                        │                │
>   │  Edit file.txt         │                │  Edit file.txt
>   │  (add "laptop edit")   │                │  (add "phone edit")
>   │                        │                │
>   │                        │ <── Upload ────│  Phone syncs first
>   │                        │    (rev 5→6)   │  (rev 6, has "phone edit")
>   │                        │                │
>   │  ✈️ Back online         │                │
>   │                        │                │
>   │── Upload (rev 5) ────> │                │
>   │   CONFLICT! Base=5,    │                │
>   │   Current=6.           │                │
>   │                        │                │
>   │  Create conflicted copy:                │
>   │  "file (Alice's laptop's conflicted     │
>   │   copy 2026-02-20).txt"                 │
> ```
>
> ### Scenario 2: Shared Folder (Different Users)
>
> ```
> Alice                    Server              Bob
>   │                        │                   │
>   │  report.pdf (rev 3)    │  (rev 3)          │  report.pdf (rev 3)
>   │                        │                   │
>   │  Edit report.pdf       │                   │  Edit report.pdf
>   │  (fix typo on page 2)  │                   │  (update chart on page 5)
>   │                        │                   │
>   │── Upload (base=rev3) ─>│                   │
>   │<── OK (rev 4) ─────── │                   │
>   │                        │                   │
>   │                        │<── Upload (base=rev3) ──│
>   │                        │   CONFLICT! Base=3,     │
>   │                        │   Current=4.            │
>   │                        │                         │
>   │                        │──── 409 Conflict ──────>│
>   │                        │                         │
>   │                        │    Bob's client creates: │
>   │                        │    "report (Bob's       │
>   │                        │     conflicted copy     │
>   │                        │     2026-02-20).pdf"    │
>   │                        │                         │
>   │                        │<── Upload conflicted ───│
>   │                        │    copy as new file     │
> ```
>
> ### Scenario 3: Offline Edits + Shared Folder
>
> ```
> This is the worst case: Alice is offline for hours while Bob,
> Charlie, and Dave all edit files in a shared folder.
>
> When Alice comes back online:
> - Queue of local changes replayed
> - Each local change checked against server state
> - Multiple conflicted copies may be created
> - Alice's sync may take minutes to resolve all conflicts
> ```

---

## 2. Optimistic vs Pessimistic Concurrency

**Interviewer:**

Why doesn't Dropbox just lock files?

**Candidate:**

> | Approach | Pessimistic (Locking) | Optimistic (Dropbox) |
> |----------|----------------------|---------------------|
> | **Mechanism** | Lock file before editing. Others blocked until lock released. | Anyone edits anytime. Detect conflicts on sync. |
> | **Offline support** | ❌ Can't acquire lock without server connectivity | ✅ Edit freely offline, resolve conflicts later |
> | **Contention** | High — forgotten locks block everyone | None — no blocking, ever |
> | **User experience** | "This file is locked by Alice" — frustrating | "Conflicted copy created" — annoying but workable |
> | **Conflict rate** | 0% (prevented by design) | < 0.1% of syncs (rare in practice) |
> | **Data loss risk** | Low (but lock expiry can cause issues) | Zero (both versions preserved) |
> | **Complexity** | Lock management (expiry, force-unlock, dead locks) | Conflict detection (rev-based CAS) |
>
> **Why optimistic wins for Dropbox:**
>
> 1. **Offline access is a core requirement.** Users edit files on airplanes, in basements, in areas with no connectivity. Locking requires server connectivity to acquire — incompatible with offline.
>
> 2. **Conflicts are rare.** At < 0.1% of sync operations, optimizing for the common case (no conflict) is correct. Locking adds overhead to 100% of operations to prevent something that happens in 0.1%.
>
> 3. **Forgotten locks are worse than conflicted copies.** A user opens a file, goes to lunch, laptop sleeps — the lock persists. All collaborators are blocked. Timeout-based lock expiry adds complexity and can cause conflicts anyway.
>
> 4. **Non-technical users.** Dropbox's user base is everyone — not just developers. "This file is locked" is confusing. "Conflicted copy" is at least visible in the file system.

---

## 3. Conflict Detection Mechanism

**Candidate:**

> Dropbox uses **revision-based Compare-And-Swap (CAS)** — identical in concept to HTTP `If-Match` ETags or database optimistic locking.
>
> ```
> The mechanism:
>
> Every file has a "rev" (revision ID) — an opaque string that
> changes on every edit. When a client uploads an edit, it includes
> the "rev" it was editing from.
>
> Upload request:
>   POST /files/upload_session/finish
>   {
>     "commit": {
>       "path": "/report.pdf",
>       "mode": { ".tag": "update", "update": "rev_003" }  ← base revision
>     }
>   }
>
> Server check:
>   current_rev = lookup_current_rev("/report.pdf")
>   if current_rev == "rev_003":          ← Match!
>     accept_upload()
>     set_rev("rev_004")                  ← Increment rev
>   else:
>     CONFLICT!                           ← Someone else already updated
>     return 409
> ```
>
> **This is identical to a CAS (Compare-And-Swap) operation:**
> ```
> CAS(expected_value=rev_003, new_value=new_data)
>   if current == expected_value:
>     current = new_data
>     return SUCCESS
>   else:
>     return FAILURE (conflict)
> ```
>
> **And identical to HTTP conditional writes:**
> ```
> PUT /report.pdf
> If-Match: "etag_003"     ← Only write if current ETag matches
>
> If match → 200 OK (write succeeds)
> If no match → 412 Precondition Failed (conflict)
> ```

---

## 4. The "Conflicted Copy" Strategy

**Candidate:**

> When a conflict is detected, Dropbox creates a **conflicted copy** — a new file containing the "losing" version:
>
> ```
> Conflict resolution:
>
> Winner (first to sync): Alice's version
>   /report.pdf → rev_004 (Alice's edits)
>
> Loser (second to sync): Bob's version
>   /report (Bob's conflicted copy 2026-02-20).pdf → rev_001
>   (new file, Bob's edits preserved as a separate file)
>
> Result:
>   /report.pdf                                    ← Alice's version (canonical)
>   /report (Bob's conflicted copy 2026-02-20).pdf ← Bob's version (preserved)
>
> Both versions are intact. Zero data loss.
> User must manually review and merge.
> ```
>
> ### Naming convention:
>
> ```
> Pattern: "{original_name} ({user}'s conflicted copy {date}).{ext}"
>
> Examples:
>   report (Bob's conflicted copy 2026-02-20).pdf
>   budget (Alice's laptop's conflicted copy 2026-02-20).xlsx
>   notes (iPad's conflicted copy 2026-02-20).txt
>
> If multiple conflicts on the same day:
>   report (Bob's conflicted copy 2026-02-20).pdf
>   report (Bob's conflicted copy 2026-02-20) (1).pdf
> ```
>
> ### Why this is the safest approach:
>
> | Property | Conflicted Copy |
> |----------|----------------|
> | **Data loss** | Zero — both versions preserved |
> | **User visibility** | High — conflicted copy appears in folder, user sees it |
> | **Complexity** | Low — no merge logic, no format-specific handling |
> | **Works for** | ALL file types (text, binary, images, Office docs, code) |
> | **Manual effort** | User must review and merge manually |
> | **Failure mode** | Extra file in folder (annoying, not dangerous) |

---

## 5. File-Type-Specific Considerations

**Interviewer:**

Could Dropbox do automatic merging for text files, like Git?

**Candidate:**

> In theory, yes. In practice, the risks outweigh the benefits for Dropbox's user base:
>
> | File Type | Could auto-merge? | Should auto-merge? | Why/Why not |
> |-----------|-------------------|-------------------|-------------|
> | **Plain text (.txt)** | Yes — line-level merge like Git | Risky — non-developers don't understand conflict markers | `<<<< HEAD` in their notes file would confuse most Dropbox users |
> | **Source code (.py, .js)** | Yes — same as Git | Maybe — developers understand merge | But Dropbox isn't a VCS; developers should use Git |
> | **Word (.docx)** | Partially — Word's Track Changes | Very risky — auto-merged XML could corrupt the document | Office XML is complex; a bad merge = unreadable document |
> | **Excel (.xlsx)** | No — cell-level merge is format-specific | No — formula dependencies make auto-merge dangerous | Merging cell A1 could break formulas in sheet 2 |
> | **Images (.psd, .jpg)** | No — binary, no meaningful diff | No | How do you "merge" two different photo edits? |
> | **PDF** | No — binary format | No | PDFs aren't editable in the same way |
> | **ZIP/archives** | No | No | Binary blob |
> | **Database files** | No — proprietary format | No | Corrupted database = total data loss |
>
> **Dropbox's position**: For a product used by hundreds of millions of non-technical users, automatic merge is too risky. A single case of corrupted-by-auto-merge data would be worse than a thousand "conflicted copy" files. **Conflicted copy is the universally safe default.**
>
> **Google's position**: Google Docs uses Operational Transforms for real-time merge — but ONLY for Google's own structured formats (Docs, Sheets, Slides). For regular files (.pdf, .zip, .psd), Google Drive falls back to **last-writer-wins** — which is WORSE than Dropbox's conflicted copy (silent data loss vs explicit both-versions-preserved).

---

## 6. Edge Cases

**Candidate:**

> ### Edge Case 1: Directory Conflicts
>
> ```
> Alice creates /NewFolder/file.txt
> Bob creates /NewFolder/data.csv
> Both sync before seeing each other's changes.
>
> No conflict! Both files are in the same folder. The folder
> is created once, both files are added. Dropbox only conflicts
> on the SAME file, not the same directory.
>
> But what if both create /NewFolder/readme.txt?
> → Conflict on readme.txt. One becomes conflicted copy.
> ```
>
> ### Edge Case 2: Delete-Edit Conflict
>
> ```
> Alice deletes report.pdf
> Bob edits report.pdf (doesn't know it's deleted)
>
> Bob uploads → server checks: file is deleted.
> Resolution: Bob's upload creates a NEW file (restores from trash).
> Alice's delete is effectively undone by Bob's edit.
>
> Rationale: preserving edits is more important than honoring deletes.
> Data > deletion intent.
> ```
>
> ### Edge Case 3: Move-Edit Conflict
>
> ```
> Alice moves report.pdf from /A/ to /B/
> Bob edits report.pdf (still thinks it's in /A/)
>
> Bob uploads → server sees the file was moved.
> Resolution: Bob's edit is applied to the file at its NEW location (/B/).
> No conflicted copy needed — the edit and the move are non-conflicting
> operations (editing content vs changing path).
> ```
>
> ### Edge Case 4: Move-Move Conflict
>
> ```
> Alice moves report.pdf from /A/ to /B/
> Bob moves report.pdf from /A/ to /C/
>
> Both sync simultaneously.
> Resolution: First-to-sync wins (file ends up in /B/ or /C/).
> Second mover gets an error or the file is re-moved.
> This is resolved by the rev-based CAS — move includes the rev.
> ```
>
> ### Edge Case 5: Rename-Rename Conflict
>
> ```
> Alice renames report.pdf to final_report.pdf
> Bob renames report.pdf to Q1_report.pdf
>
> CAS on rev: first rename wins. Second rename fails → client
> discovers the new name → can rename from the new name if desired.
> ```

---

## 7. Conflict Rate Analysis

**Candidate:**

> Why is the conflict rate so low (< 0.1%)?
>
> ```
> Model: two users sharing a folder with 100 files.
>
> Assumptions:
> - Each user edits ~5 files per day
> - Average time between edit and sync: 10 seconds
> - Edit sessions: ~30 minutes each
>
> For a conflict to occur:
> - Both users must edit the SAME file (5/100 = 5% chance per edit)
> - Edits must overlap in time (10 sec overlap window / 86,400 sec per day = 0.012%)
> - Combined probability: 5% × 0.012% ≈ 0.0006%
>
> Even with generous assumptions:
> - 10 shared files (more focused collaboration): 50% × 0.012% = 0.006%
> - Longer sync delay (60 seconds): 50% × 0.07% = 0.035%
>
> Result: conflicts are rare enough that the "conflicted copy" approach
> doesn't create significant user friction.
>
> Compare to Google Docs:
> - Multiple users with cursors in the same document simultaneously
> - Edits overlap constantly (that's the point of real-time collaboration)
> - Conflict rate would be near 100% without OT → OT is essential
>
> Different products, different conflict frequencies, different solutions.
> ```

---

## Contrast: Conflict Resolution Approaches

| Approach | Used By | Mechanism | Data Loss | User Experience | Works For |
|----------|---------|-----------|-----------|-----------------|-----------|
| **Conflicted Copy** | Dropbox | Preserve both versions as separate files | Zero | Manual merge required | All file types |
| **Last-Writer-Wins** | Google Drive (non-Docs), S3 | Latest upload silently overwrites | Yes — earlier edit lost | Seamless but dangerous | When data loss is acceptable |
| **Operational Transforms** | Google Docs, Figma | Real-time character/operation-level merge | Zero | Best UX — simultaneous editing | Structured documents (text, spreadsheets) |
| **Three-Way Merge** | Git | Common ancestor + both versions → merged result | Low (text), manual intervention for conflicts | Developer-friendly | Source code, text files |
| **CRDTs** | Figma, some collaborative editors | Mathematically convergent data structures | Zero | Automatic merge, no central server needed | Specific data structures (counters, sets, text) |
| **Pessimistic Locking** | SVN, Perforce, traditional databases | Lock before edit, block others | Zero | "File is locked" — blocking | When coordination is acceptable |
>
> ### When each approach is the right choice:
>
> ```
> Dropbox (conflicted copy):
>   ✅ Arbitrary binary files
>   ✅ Non-technical users
>   ✅ Offline support required
>   ✅ Conflicts are rare
>
> Google Docs (OT):
>   ✅ Structured documents
>   ✅ Real-time collaboration is the core feature
>   ✅ Always-online usage
>   ❌ Doesn't work for binary files
>
> Git (three-way merge):
>   ✅ Source code
>   ✅ Developers who understand merge
>   ✅ Branch-based workflows
>   ❌ Too complex for non-developers
>
> Pessimistic locking:
>   ✅ High-value documents (legal contracts, CAD files)
>   ✅ When coordination overhead is acceptable
>   ❌ Incompatible with offline access
>   ❌ Forgotten locks block everyone
> ```

---

## 8. Vector Clocks vs Simple Revisions

**Interviewer:**

Why does Dropbox use simple revision IDs instead of vector clocks?

**Candidate:**

> **Vector clocks** track `{deviceA: version3, deviceB: version5}` — they can detect **concurrent** edits (neither is "newer") without a central coordinator.
>
> **Dropbox doesn't need this** because it has a **centralized server** as the single source of truth:
>
> ```
> Vector clocks (decentralized):
>   Alice's clock: {alice: 3, bob: 2}
>   Bob's clock:   {alice: 2, bob: 3}
>   → Neither dominates → concurrent edit detected
>
>   Useful in: peer-to-peer systems (Dynamo, Riak)
>   where there's no central authority.
>
> Dropbox revision (centralized):
>   Server says: current rev = 5
>   Alice uploads with base rev = 5 → accepted, rev becomes 6
>   Bob uploads with base rev = 5 → rejected (5 ≠ 6, conflict!)
>
>   The server IS the authority. No need for vector clocks.
> ```
>
> | Approach | When to use | Complexity |
> |----------|-------------|------------|
> | **Simple rev (Dropbox)** | Centralized server is single source of truth | Low — one opaque string per file |
> | **Vector clock** | Peer-to-peer, no central server (Dynamo, Riak) | High — O(N) where N = number of nodes/clients |
> | **Lamport timestamp** | Ordering events in distributed system | Medium — single counter, no conflict detection |
> | **Hybrid logical clock** | CockroachDB, Spanner — global ordering with bounded skew | High — requires clock synchronization |

---

## L5 vs L6 vs L7 — Conflict Resolution Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|--------|------------|------------|-----------------|
| **Detection** | "Check if file was modified" | Designs rev-based CAS, maps to HTTP If-Match ETag, explains optimistic concurrency | Compares vector clocks vs simple revisions, explains why centralized = simple rev, decentralized = vector clocks |
| **Resolution** | "Keep the latest version" | Designs conflicted copy approach, explains naming convention, why both-versions-preserved is safest | Analyzes file-type-specific options (text merge, OT for docs), explains why universal conflicted copy is the right default for non-technical users |
| **Conflict rate** | "Conflicts happen sometimes" | Knows conflicts are rare, mentions < 0.1% | Models conflict probability mathematically (overlap window × same-file probability), explains why Dropbox's model degrades for large teams |
| **Edge cases** | Not considered | Handles delete-edit (preserve edits over deletes) | Full edge case analysis: move-edit, move-move, rename-rename, delete-edit, directory conflicts |
| **Contrast** | None | Contrasts with Git (three-way merge) | Full comparison matrix: conflicted copy, LWW, OT, CRDT, pessimistic locking — explains when each is the right choice |

---

> **Summary:** Dropbox uses optimistic concurrency with rev-based CAS for conflict detection and "conflicted copy" for resolution. This approach prioritizes data safety (zero data loss) over user convenience (manual merge required). It works well because: (1) conflicts are rare (< 0.1% of syncs), (2) it works for ALL file types (binary, text, structured), (3) it's compatible with offline access, and (4) it's simple to implement and reason about. The alternatives — OT (Google Docs), three-way merge (Git), CRDTs (Figma) — are better for specific use cases (real-time collaboration, source code) but don't generalize to arbitrary binary files used by non-technical users.
