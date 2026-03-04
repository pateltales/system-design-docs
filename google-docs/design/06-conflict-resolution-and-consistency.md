# Deep Dive: Conflict Resolution & Consistency

> **Companion document to [01-interview-simulation.md](01-interview-simulation.md) -- Phase 8**
> This document expands on the conflict resolution and consistency discussion from the main interview simulation.

---

## Table of Contents

1. [Centralized OT Convergence](#1-centralized-ot-convergence)
2. [Client State Machine](#2-client-state-machine)
3. [Server-Side Transform Process](#3-server-side-transform-process)
4. [Consistency Guarantees](#4-consistency-guarantees)
5. [Concrete Conflict Examples](#5-concrete-conflict-examples)
6. [Network Partitions and Disconnection](#6-network-partitions-and-disconnection)
7. [Contrast with Other Systems](#7-contrast-with-other-systems)

---

## 1. Centralized OT Convergence

### The Single Source of Truth

Google Docs uses a **centralized, server-authoritative** collaboration model. The server is the single source of truth for the document state. This is the foundation of the consistency model:

```
                     Central OT Server
                    (single source of truth)
                           |
              ┌────────────┼────────────┐
              |            |            |
           Client A    Client B    Client C
           (local       (local       (local
            copy)        copy)        copy)

Rules:
  1. All operations pass through the server.
  2. The server assigns a TOTAL ORDER to all operations.
  3. All clients apply operations in the SAME order.
  4. Therefore, all clients converge to the SAME state.
```

### Total Ordering

The server maintains a monotonically increasing **revision counter**. Each operation that the server accepts is assigned the next revision number:

```
Server revision timeline:

  Rev 1:  insert("Hello")                 from Alice
  Rev 2:  retain(5), insert(" World")     from Alice
  Rev 3:  retain(5), delete(6)            from Bob
  Rev 4:  retain(5), insert(" Docs")      from Bob
  Rev 5:  format(0, 5, {bold: true})      from Alice
  Rev 6:  retain(10), insert("!")         from Carol

  Every client eventually receives ALL operations in this
  exact order: 1, 2, 3, 4, 5, 6.

  This total ordering eliminates the need for vector clocks,
  conflict detection, or consensus protocols. The server IS
  the consensus -- it decides the order.
```

### Why Centralized is Simpler

```
Centralized OT (Google Docs):
  - Server imposes total order → only need TP1 (one transform property)
  - TP1: transform(A, B) → (A', B') such that apply(A) then apply(B')
         equals apply(B) then apply(A')
  - TP1 is well-understood, implementable, testable

Decentralized OT (peer-to-peer):
  - No central authority → need TP1 AND TP2
  - TP2: transform is associative when composing multiple transforms
  - TP2 is EXTREMELY hard to implement correctly
  - Published algorithms with claimed TP2 proofs have been shown
    to be INCORRECT (Imine et al., 2003; Oster et al., 2006)
  - Google Wave attempted decentralized OT on XML trees
    → overwhelming complexity → project abandoned

Google's decision: "We run reliable servers. We don't NEED
decentralization. TP1-only centralized OT is simpler, provably
correct, and performant at our scale."
```

---

## 2. Client State Machine

### The Three States

Every Google Docs client maintains a state machine with exactly three states, based on the Jupiter protocol (Nichols et al., 1995) as refined in the Google Wave OT whitepaper (David Wang, 2010):

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                  │
│                     CLIENT STATE MACHINE                         │
│                                                                  │
│  ┌──────────────────────┐                                        │
│  │     SYNCHRONIZED     │ ◄──────────────────────────────────┐   │
│  │                      │                                     │   │
│  │ No pending ops.      │                                     │   │
│  │ Client rev = Server  │   ACK received for in-flight op     │   │
│  │ rev.                 │   AND buffer is empty.              │   │
│  │                      │                                     │   │
│  └──────────┬───────────┘                                     │   │
│             │                                                  │   │
│             │ User makes an edit                               │   │
│             │ → Apply locally (optimistic)                     │   │
│             │ → Send operation to server                       │   │
│             │                                                  │   │
│             v                                                  │   │
│  ┌──────────────────────┐                                     │   │
│  │    AWAITING ACK      │                                     │   │
│  │                      │                                     │   │
│  │ 1 operation "in      │  ACK received for in-flight op     │   │
│  │ flight" (sent to     │  AND buffer is empty.              │   │
│  │ server, not yet      │  → Move to SYNCHRONIZED  ──────────┘   │
│  │ acknowledged).       │                                        │
│  │                      │                                        │
│  │ No buffered ops.     │                                        │
│  │                      │                                        │
│  └──────────┬───────────┘                                        │
│             │                                                     │
│             │ User makes ANOTHER edit                             │
│             │ → Apply locally (optimistic)                        │
│             │ → BUFFER the new op (don't send yet)               │
│             │                                                     │
│             v                                                     │
│  ┌──────────────────────┐                                        │
│  │ AWAITING ACK +       │                                        │
│  │ BUFFER               │                                        │
│  │                      │  ACK received for in-flight op:        │
│  │ 1 op in flight.      │  → Send buffer as new in-flight op    │
│  │ 1 op in buffer.      │  → Move to AWAITING ACK  ──────┐      │
│  │                      │                                 │      │
│  │ If user edits MORE:  │                                 │      │
│  │ → COMPOSE new edit   │                                 │      │
│  │   into buffer.       │                                 │      │
│  │ → Buffer stays as    │                                 │      │
│  │   ONE composed op.   │                                 │      │
│  │                      │                                 │      │
│  └──────────────────────┘                                 │      │
│                                                           │      │
│                          ┌────────────────────────────────┘      │
│                          │                                        │
│                          v                                        │
│                 ┌──────────────────────┐                          │
│                 │    AWAITING ACK      │                          │
│                 │ (buffer was sent)    │ ─── (and so on) ────────┘│
│                 └──────────────────────┘                          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Why Only One In-Flight Operation?

This is a key protocol design decision:

```
WHY NOT allow multiple in-flight operations?

  If we allowed 5 in-flight ops (A, B, C, D, E):
    - Server receives A, transforms against concurrent ops, applies.
    - Server receives B, transforms against concurrent ops AND A's result.
    - Server receives C, transforms against concurrent ops AND A AND B's results.
    - ...
    - The server must track all in-flight ops per client.
    - The client must track all in-flight ops for incoming transforms.
    - Complexity: O(K * N) where K = in-flight ops, N = concurrent server ops.

  With ONE in-flight + ONE buffer:
    - The buffer COMPOSES multiple edits into ONE operation.
    - Composition: combine(insert(5,"X"), insert(6,"Y")) → insert(5,"XY")
    - No matter how many local edits the user makes while waiting
      for an ACK, the buffer is always ONE composed operation.
    - Result: at most 2 operations to track (in-flight + buffer).
    - Complexity: O(2 * N) = O(N), regardless of typing speed.

  This is elegant: the protocol complexity is bounded,
  while the user can type as fast as they want.
```

### Handling Incoming Server Operations

The most subtle part of the client state machine is what happens when the server sends an operation from another user while the client has pending (in-flight or buffered) operations:

```
CASE: Client is in AWAITING ACK + BUFFER state
  In-flight: op_A (sent to server, not yet ACK'd)
  Buffer:    op_B (not yet sent)
  Server sends: op_S (from another user, already applied on server)

  The client needs to:
    1. Apply op_S to its local document
    2. But the local document already has op_A and op_B applied
       (optimistically). op_S doesn't account for these.

  Solution: Transform op_S against the client's pending ops.

  Step 1: transform(op_A, op_S) → (op_A', op_S')
    op_A' = our in-flight op as the server will see it
            (already handled by server -- we don't use this)
    op_S' = server's op, adjusted for our in-flight op

  Step 2: transform(op_B, op_S') → (op_B', op_S'')
    op_S'' = server's op, adjusted for BOTH our in-flight AND buffer

  Step 3: Apply op_S'' to local document
    This correctly accounts for our pending ops.

  Step 4: Update our pending ops:
    in-flight = op_A   (unchanged -- already sent to server)
    buffer    = op_B'  (transformed -- will be sent after ACK)


  Diagram (diamond property):

       Client state        Server state
           |                    |
           | op_A (in-flight)   |
           |───────────────────>|
           |                    | op_S (from another user)
           |<───────────────────|
           |                    |
     Local: op_A, op_B    Server: op_S
     applied               applied
           |                    |
     Transform:            Transform:
     op_S against          op_A against
     op_A, op_B            op_S
           |                    |
     Apply op_S''          Apply op_A'
     locally               on server
           |                    |
           v                    v
     SAME DOCUMENT STATE (convergence!)
```

---

## 3. Server-Side Transform Process

### Step-by-Step Server Processing

When the server receives an operation from a client, it follows this exact process:

```
SERVER RECEIVES op_X from Client X, based on revision r.
Server is currently at revision r + n (n operations have happened since rev r).

Step 1: IDENTIFY THE GAP
  The client sent op_X based on revision r.
  The server has applied n more operations since revision r:
    op_S1, op_S2, ..., op_Sn (at revisions r+1, r+2, ..., r+n)

  These are the "intervening operations" that op_X doesn't know about.

Step 2: TRANSFORM AGAINST INTERVENING OPERATIONS
  Transform op_X against each intervening operation, sequentially:

    op_X_1 = transform(op_X,   op_S1)   // adjust for first intervening op
    op_X_2 = transform(op_X_1, op_S2)   // adjust for second
    op_X_3 = transform(op_X_2, op_S3)   // adjust for third
    ...
    op_X_n = transform(op_X_{n-1}, op_Sn) // adjust for all n

  op_X_n is the TRANSFORMED operation -- it produces the same
  intended effect as the original op_X, but on the current
  server document state.

Step 3: APPLY THE TRANSFORMED OPERATION
  Apply op_X_n to the server's document state.
  Increment server revision: r + n + 1

Step 4: APPEND TO OPERATION LOG
  Persist op_X_n in the operation log at revision r + n + 1.
  This is the durable record.

Step 5: BROADCAST TO OTHER CLIENTS
  Send op_X_n to all other connected clients (Client Y, Client Z, ...).
  They will apply this operation to their local document state
  (after transforming against their own pending operations).

Step 6: ACK TO CLIENT X
  Send ACK to Client X with the assigned revision number (r + n + 1).
  Client X now knows its operation was accepted and at what revision.
  Client X transitions from AWAITING ACK to:
    - SYNCHRONIZED (if no buffer)
    - AWAITING ACK (if buffer was promoted to in-flight)


WORKED EXAMPLE:

  Server state: "ABCDEFGH" at revision 10

  Alice sends: insert("X", pos=5) based on revision 8
    (Alice hasn't received revisions 9 and 10 yet)

  Intervening operations (rev 9 and 10):
    Rev 9:  Bob's   delete(pos=3, count=1)   → "ABCEFGH" (deleted D)
    Rev 10: Carol's insert("Z", pos=0)       → "ZABCEFGH" (inserted Z at start)

  Transform Alice's insert(5, "X") against rev 9 (delete at pos 3):
    Delete at pos 3 is BEFORE insert at pos 5.
    Shift insert position left by 1 (one char deleted before pos 5).
    Result: insert(4, "X")

  Transform insert(4, "X") against rev 10 (insert "Z" at pos 0):
    Insert at pos 0 is BEFORE insert at pos 4.
    Shift insert position right by 1 (one char inserted before pos 4).
    Result: insert(5, "X")

  Apply insert(5, "X") to server state "ZABCEFGH":
    Result: "ZABCEXFGH"
    New revision: 11

  Broadcast insert(5, "X") to Bob and Carol.
  ACK to Alice with revision 11.
```

### Transform Complexity

```
Transform cost for one incoming operation:

  If client is behind by n revisions:
    n transform function calls required.

  Each transform call: O(L) where L = operation length
    (walking the retain/insert/delete components)

  Total: O(n * L)

  Typical case: client is 0-5 revisions behind → negligible cost
  Worst case: client was offline for hours, behind by 10,000 revisions
    → 10,000 transforms → potentially seconds of computation
    (see Section 6: Network Partitions)

  Per-document limit:
    At 500 ops/sec (100 editors × 5 ops/sec), the server processes
    each incoming op within ~1ms. Transforms against 1-5 intervening
    ops add < 1ms. Total: ~2ms per operation. Easily sustainable.
```

---

## 4. Consistency Guarantees

The OT system provides three specific consistency guarantees. These are NOT the same as distributed systems consistency levels (linearizability, serializability, etc.), though there are relationships:

### Guarantee 1: Convergence

```
CONVERGENCE:
  All clients reach the same document state, given that they have
  received and applied the same set of operations.

  Formally:
    If Client A and Client B have both applied operations
    {op1, op2, ..., opN} (possibly in different orders due to
    OT transforms), their document states are IDENTICAL.

  This is guaranteed by:
    1. Total ordering by the server (all clients see the same order)
    2. TP1 transform property (applying ops in different orders
       with correct transforms yields the same result)

  CONVERGENCE ≠ EVENTUAL CONSISTENCY:
    Eventual consistency: replicas eventually agree, no guarantee
    on WHAT they agree on.
    OT Convergence: replicas eventually agree on a SPECIFIC state
    that preserves ALL operations' effects.
```

### Guarantee 2: Intention Preservation

```
INTENTION PRESERVATION:
  Each operation's intended effect is preserved, even when
  concurrent operations shift positions.

  Example:
    Alice intends to bold "the quick brown fox" (chars 10-29).
    Bob inserts "very " at position 15 (between "quick" and "brown").

    WITHOUT intention preservation:
      Alice's bold(10, 29) is applied literally.
      "the quick " is bolded (10-14) and "brown fox" starts at 15.
      But Bob's "very " at 15-19 is NOT bolded.
      Alice's intent (bold the phrase including "brown") is violated.

    WITH intention preservation (OT):
      Alice's bold is transformed against Bob's insert:
      bold(10, 29) → bold(10, 34)
      "the quick very brown fox" is ALL bolded.
      Alice's intent is preserved: the text she meant to bold IS bolded.

  Intention preservation is not mathematically guaranteed by TP1 alone.
  It is a DESIGN GOAL of the transform functions -- the transform
  logic is written to preserve human intent, not just achieve convergence.

  Cases where intention preservation is ambiguous:
    - Alice bolds chars 10-20. Bob deletes chars 15-25.
      What happens to the bold? Bold chars 10-15 remain.
      Chars 15-20 (bolded by Alice, deleted by Bob) are gone.
      Is Alice's intent preserved? Partially -- the surviving
      text that she bolded IS still bold.
```

### Guarantee 3: Causality

```
CAUSALITY:
  If operation A causally precedes operation B (A happened before B,
  and B may depend on A), then all clients see A before B.

  The centralized server trivially provides causality:
    - All operations are totally ordered by the server.
    - If A is at revision 5 and B is at revision 8,
      ALL clients see A before B.

  Why this matters:
    Alice types "TODO: fix this bug" (operation A).
    Bob reads Alice's text and adds a comment: "I'll fix it" (operation B).
    Causality ensures that on ALL clients, Alice's text appears
    BEFORE Bob's comment. If Bob's comment appeared before
    Alice's text, it would be nonsensical.

  In a decentralized system (CRDTs), causality requires
  vector clocks or Lamport timestamps. In centralized OT,
  the server's total ordering provides causality for free.
```

### Relationship to Distributed Systems Consistency

```
OT Convergence vs Traditional Consistency Models:

  Model               Guarantee                   OT Equivalent
  ────────────────────────────────────────────────────────────────
  Linearizability     Every op appears to take     Server's total
                      effect at a single point     ordering provides
                      in time, in real-time order  this for operations
                                                   ON THE SERVER.
                                                   Clients see
                                                   operations with
                                                   delay.

  Sequential          All ops appear in some       Yes -- the server's
  Consistency         sequential order consistent  revision sequence
                      with program order           IS the sequential
                                                   order.

  Causal              Causally related ops seen    Yes -- total ordering
  Consistency         in causal order              implies causal ordering.

  Eventual            Replicas eventually agree    Yes, but STRONGER.
  Consistency                                      OT convergence
                                                   preserves intent,
                                                   not just agreement.

  OT Convergence is STRONGER than eventual consistency:
    - Eventual consistency: "eventually all replicas agree on SOMETHING"
    - OT convergence: "all replicas agree on a SPECIFIC state that
      reflects ALL operations with their intended effects preserved"
```

---

## 5. Concrete Conflict Examples

### Conflict 1: Insert-Insert at the Same Position

```
Document: "ABCDEFGH" (8 chars)
Server revision: 10

Alice sends: insert("X", pos=5) based on rev 10
Bob sends:   insert("Y", pos=5) based on rev 10

Both want to insert at position 5 (between E and F).

Server receives Alice first:
  Apply insert("X", pos=5) → "ABCDEXFGH" → rev 11

Server receives Bob (based on rev 10, server at rev 11):
  Transform insert("Y", pos=5) against insert("X", pos=5):

  SAME POSITION → TIEBREAK needed.
  Tiebreak rule: lower userId goes first.

  Case: "alice" < "bob" (lexicographic comparison)
    Alice's insert is "before" Bob's.
    Bob's insert shifts right: insert("Y", pos=6)

  Apply insert("Y", pos=6) → "ABCDEXYFGH" → rev 12

  Final: "ABCDEXYFGH"
    Alice's X is at position 5.
    Bob's Y is at position 6.
    Deterministic on ALL clients.

  If Bob's message arrived first:
    Apply Bob's insert("Y", pos=5) → "ABCDEYFGH" → rev 11
    Transform Alice's insert("X", pos=5) against insert("Y", pos=5):
      Tiebreak: "alice" < "bob" → Alice goes first
      Alice's insert stays at pos=5: insert("X", pos=5)
    Apply: "ABCDEXYFGH" → rev 12

    SAME RESULT regardless of arrival order. TP1 holds.
```

### Conflict 2: Delete-Delete with Overlapping Ranges

```
Document: "ABCDEFGHIJ" (10 chars)
Server revision: 20

Alice sends: delete(pos=3, count=4) based on rev 20
  Intent: delete chars 3,4,5,6 → delete "DEFG"

Bob sends:   delete(pos=5, count=4) based on rev 20
  Intent: delete chars 5,6,7,8 → delete "FGHI"

Overlap: chars 5 and 6 ("FG") are in BOTH delete ranges.

Server receives Alice first:
  Apply delete(3, 4) → "ABCHIJ" → rev 21
  Document is now 6 chars.

Server receives Bob (based on rev 20, server at rev 21):
  Transform delete(5, 4) against delete(3, 4):

  Alice deleted chars [3,4,5,6].
  Bob wants to delete chars [5,6,7,8].

  Chars 5,6: ALREADY DELETED by Alice → skip these.
  Chars 7,8: Still exist but shifted.
    Original pos 7 → new pos 3 (shifted left by 4 deletions)
    Original pos 8 → new pos 4

  Transformed: delete(pos=3, count=2)

  Apply delete(3, 2) → "ABCJ" → rev 22

Verification:
  Original: "ABCDEFGHIJ"
  Alice deleted "DEFG": "ABCHIJ"
  Bob deleted "HI" (the surviving parts of his original range): "ABCJ"

  Combined intent: delete "DEFGHI" → "ABCJ" ✓
  No double-deletion. No missing deletion. Correct.
```

### Conflict 3: Format Conflict (Bold + Italic on Same Range)

```
Document: "ABCDEFGH" (8 chars, no formatting)
Server revision: 30

Alice sends: format(pos=2, len=4, {bold: true}) based on rev 30
  Intent: bold chars 2,3,4,5 → bold "CDEF"

Bob sends:   format(pos=2, len=4, {italic: true}) based on rev 30
  Intent: italicize chars 2,3,4,5 → italicize "CDEF"

Server receives Alice first:
  Apply format(2, 4, {bold: true}) → chars "CDEF" are now bold → rev 31

Server receives Bob (based on rev 30, server at rev 31):
  Transform format(2, 4, {italic: true}) against format(2, 4, {bold: true}):

  Both operations target the same range [2, 6).
  Bold and italic are INDEPENDENT formatting attributes.
  No conflict: both can be applied.

  Transformed: format(2, 4, {italic: true}) → UNCHANGED
  (Format transforms against other formats are usually no-ops
   when the attributes are independent.)

  Apply format(2, 4, {italic: true}) → chars "CDEF" are now bold AND italic → rev 32

Final: "CDEF" is bold italic. Both users' intentions are preserved.

This is a NON-conflicting conflict. The transform function
recognizes that independent attributes can coexist.
```

### Conflict 4: Comment Anchor on Deleted Text

```
Document: "The quick brown fox jumps over the lazy dog."
Server revision: 40

Alice sends: addComment(anchorStart=10, anchorEnd=29,
             text="This is a great phrase!") based on rev 40
  Intent: Comment on "quick brown fox jumps" (chars 10-29)

Bob sends:   delete(pos=4, count=30) based on rev 40
  Intent: Delete "quick brown fox jumps over the" (chars 4-33)

Server receives Bob first:
  Apply delete(4, 30) → "The lazy dog." → rev 41

Server receives Alice (based on rev 40, server at rev 41):
  Transform addComment(10, 29, ...) against delete(4, 30):

  The comment anchor [10, 29] is ENTIRELY WITHIN the deleted range [4, 33].
  ALL of the commented text has been deleted.

  Options:
    a) Delete the comment too → BAD. Alice's comment is lost silently.
       Bob may not have even seen the comment.

    b) Orphan the comment → GOOD. The comment survives, but its
       anchor is invalid. Display: "The text you commented on was deleted."
       Alice can see her comment. She can reply, resolve, or re-anchor it.

    c) Reject Alice's comment → BAD. Alice sent it before knowing
       about Bob's delete. Rejecting it violates intent preservation.

  Google Docs chooses option (b): ORPHAN the comment.

  Transformed comment: anchorStart=4, anchorEnd=4 (zero-width anchor
  at the deletion point), marked as "orphaned."

  Result:
    Document: "The lazy dog."
    Comment by Alice: "This is a great phrase!"
      Status: Orphaned (anchor text was deleted)
      Displayed: "The commented text was deleted." with a link
      to the comment thread.
```

### Conflict 5: Permission Change During Active Editing

```
Document: "Meeting Notes" edited by Alice (Editor role)
Server revision: 50

Timeline:
  t=0ms:    Owner sends: PATCH /documents/{docId}/permissions
            Change Alice's role: Editor → Viewer

  t=5ms:    Alice sends: insert("Important: ", pos=0) based on rev 50
            (Alice doesn't know her permissions changed yet)

  t=10ms:   Permission change is written to Spanner (strongly consistent)

  t=15ms:   Server receives Alice's insert(0, "Important: ")
            Server checks permissions: Alice is now a VIEWER
            → REJECT the operation. Send error to Alice.

  t=20ms:   Server pushes permission change notification to Alice
            via WebSocket:
            {
              "type": "permission_change",
              "newRole": "viewer"
            }

  t=25ms:   Alice's client receives the rejection AND the permission change:
            1. Undo the optimistically applied insert ("Important: " disappears)
            2. Switch to read-only mode
            3. Show notification: "Your access has been changed to Viewer"

  t=30ms:   Any operations in Alice's buffer are DISCARDED.
            Alice's client enters SYNCHRONIZED state (read-only).

Key points:
  - The server ALWAYS checks permissions on every operation.
  - Optimistically applied operations are rolled back if rejected.
  - Spanner's strong consistency ensures no window where the old
    permission is visible after the change.
  - This is defense-in-depth: even a malicious client that ignores
    the permission change notification cannot edit the document.
```

### Conflict 6: Concurrent Insert and Format on Overlapping Range

```
Document: "ABCDEFGHIJ" (10 chars)
Server revision: 60

Alice sends: insert("XY", pos=5) based on rev 60
  Intent: insert "XY" between E and F

Bob sends:   format(pos=3, len=5, {bold: true}) based on rev 60
  Intent: bold chars 3-7 → bold "DEFGH"

Server receives Alice first:
  Apply insert("XY", pos=5) → "ABCDEXYFGHIJ" → rev 61

Server receives Bob (based on rev 60, server at rev 61):
  Transform format(3, 5, {bold:true}) against insert("XY", pos=5):

  Alice inserted 2 chars at pos 5, which is WITHIN Bob's format range [3, 8).
  Bob's format range must EXPAND to include the inserted characters
  (they are "inside" the range Bob intended to bold).

  Transformed: format(3, 7, {bold: true})
    Original range: [3, 8) = "DEFGH" (5 chars)
    Expanded range: [3, 10) = "DEXYFGH" (7 chars)

  Apply: "DEXYFGH" all become bold.

  Result: "ABC|DEXYFGH|IJ" where | marks the bold boundary.

  Is this correct? Bob intended to bold "DEFGH."
  Alice inserted "XY" inside the bold region.
  Should "XY" be bold? YES -- it was inserted within a bold range.
  The expanded format preserves Bob's intention.
```

### Conflict 7: Undo in the Presence of Concurrent Operations

```
Document: "Hello World" (11 chars)
Server revision: 70

Alice types "X" at pos 5 → "HelloX World" → rev 71
Bob types "Y" at pos 0  → "YHelloX World" → rev 72

Alice presses Ctrl+Z (undo her insert of "X"):
  The naive approach: delete char at pos 5.
  But pos 5 is now "o" (not "X"), because Bob's insert shifted everything.

  The correct approach: TRANSFORM the inverse operation.
    Alice's original op: insert("X", pos=5) at rev 70
    Inverse: delete(pos=5, count=1)
    Transform inverse against rev 71 (already done -- this IS rev 71)
    Transform inverse against rev 72 (Bob's insert at pos 0):
      Insert at pos 0 is before pos 5 → shift right
      Transformed inverse: delete(pos=6, count=1)

    Apply delete(6, 1) → "YHello World" → rev 73

  Result: "YHello World" -- Alice's "X" is removed, Bob's "Y" remains.
  OT-aware undo correctly reverses Alice's edit without affecting Bob's.

  This is why undo in collaborative editing is NON-TRIVIAL.
  You cannot simply "undo the last operation." You must:
    1. Compute the inverse of the operation to undo.
    2. Transform the inverse against ALL operations that
       happened after the original.
    3. Apply the transformed inverse as a NEW operation
       (which goes through the normal OT pipeline).
```

---

## 6. Network Partitions and Disconnection

### Offline Editing Creates Divergence

When a client loses network connectivity, it continues editing locally (offline mode). This creates a divergence between the client's local state and the server's state:

```
Timeline of an offline editing session:

  t=0:      Alice's connection drops. Server rev = 100.
  t=0-2hr:  Alice edits offline. Creates 500 local operations.
            Alice's local doc = server doc at rev 100 + 500 local ops.

  t=0-2hr:  Bob and Carol continue editing online.
            Server processes 2,000 operations from Bob and Carol.
            Server rev = 2,100.

  t=2hr:    Alice reconnects.

  Divergence:
    Server: rev 100 + 2,000 server ops = rev 2,100
    Alice:  rev 100 + 500 local ops = rev 100 + 500 (unsent)

    Alice's local state and the server's state have DIVERGED.
    Reconciliation required.
```

### Reconnection Sync Process

```
Alice reconnects after 2 hours offline:

Step 1: Establish WebSocket connection.
  Client sends: "I'm at revision 100, I have 500 pending operations."

Step 2: Server identifies the gap.
  Server is at revision 2,100.
  Gap: 2,000 operations (rev 101 through rev 2,100).

Step 3: Server transforms Alice's 500 ops against 2,000 server ops.
  For EACH of Alice's 500 operations:
    Transform against ALL 2,000 server operations.

  Total transform calls: 500 × 2,000 = 1,000,000

  At ~1 microsecond per transform: ~1 second of computation.
  At ~10 microseconds per transform: ~10 seconds.

  This is the O(M × N) cost of offline reconciliation.
    M = offline operations from Alice
    N = server operations during offline period

Step 4: Server applies all 500 transformed operations.
  Each is appended to the operation log.
  Server rev: 2,100 + 500 = 2,600

Step 5: Server sends 2,000 operations to Alice.
  (Transformed for her context.)
  Alice applies them to her local document.

Step 6: Convergence.
  Alice's local state = Server state at rev 2,600.
  Bob and Carol's states also converge (they received Alice's ops
  via normal broadcast).

During reconciliation, Alice's UI shows "Syncing..."
```

### The O(M x N) Problem

```
Reconnection transform cost:

  Offline ops (M)    Server ops (N)    Transforms    Estimated time
  ──────────────────────────────────────────────────────────────────
       10                 50              500         < 1ms
       50                500           25,000         ~25ms
      100              1,000          100,000         ~100ms
      500              2,000        1,000,000         ~1-10 sec
    1,000              5,000        5,000,000         ~5-50 sec
    5,000             10,000       50,000,000         minutes

  Mitigation strategies:

  1. COMPOSE offline operations:
     Before sending 500 individual ops to the server,
     compose them into fewer, larger operations.
     If 500 ops can be composed into 50 compound ops,
     the transform cost drops by 10x.

  2. BATCH server operations:
     Instead of transforming against each of 2,000 individual ops,
     compose the 2,000 server ops into a smaller number of
     compound operations. Reduces N.

  3. LIMIT offline duration:
     Show a warning after N minutes of offline editing:
     "You've been offline for a while. Some changes may be
      difficult to merge when you reconnect."

  4. SNAPSHOT-based reconciliation:
     Instead of transforming ops, take a snapshot of the server state
     and diff it against the client state. This is an approximation
     that may lose some fine-grained intention preservation but is
     much faster.
```

### User Experience During Reconciliation

```
What Alice sees when she reconnects after 2 hours offline:

  1. "Syncing your changes..." progress bar.
     (Server is transforming her 500 ops against 2,000 server ops.)

  2. Document content CHANGES as server ops are applied.
     Alice may see text appearing, formatting changing,
     new comments showing up.

  3. Alice's edits are PRESERVED but may be INTERLEAVED
     with Bob and Carol's edits.

     Before offline: "The project plan has three phases."
     Alice's offline edit: "The REVISED project plan has THREE phases."
     Bob's online edit: "The project plan has three critical phases."

     After reconciliation:
     "The REVISED project plan has THREE critical phases."
     (Both Alice's "REVISED" and "THREE" and Bob's "critical"
      are preserved. OT interleaves them correctly.)

  4. Alice may be surprised by the result.
     Her carefully crafted paragraph now has Bob's additions
     mixed in. This is mathematically correct (convergence)
     but may not match Alice's expectation.

  5. The "Version History" shows Alice's offline edits as a batch:
     "Alice Chen (500 edits while offline)"
     Alice can review what changed and manually adjust if needed.
```

---

## 7. Contrast with Other Systems

### Google Docs vs Dropbox (Conflicted Copies)

```
+----------------------------+----------------------------+
|        GOOGLE DOCS         |        DROPBOX             |
+----------------------------+----------------------------+
| Conflict resolution:       | Conflict resolution:       |
| AUTOMATIC via OT.          | MANUAL via conflicted      |
| All conflicts are resolved | copies. If Alice and Bob   |
| in real-time. Users never  | edit the same file, Dropbox|
| see a conflict.            | creates "File (Alice's     |
|                            | conflicted copy)" that the |
|                            | user must manually merge.  |
+----------------------------+----------------------------+
| Granularity: CHARACTER     | Granularity: WHOLE FILE    |
| LEVEL. Each character      | A one-character change     |
| insertion/deletion is      | triggers a full file sync. |
| tracked independently.     | No sub-file diffing.       |
+----------------------------+----------------------------+
| Real-time: YES.            | Real-time: NO.             |
| Edits appear on other      | Edits sync when saved.     |
| clients within 200ms.      | Seconds to minutes delay.  |
+----------------------------+----------------------------+
| Data loss: NONE.           | Data loss: POSSIBLE.       |
| Every edit from every user | Last-writer-wins for       |
| is preserved by OT.        | non-conflicting saves.     |
|                            | Conflicted copies may be   |
|                            | ignored or deleted by the  |
|                            | user, losing edits.        |
+----------------------------+----------------------------+
| Product model: DOCUMENT    | Product model: FILE SYNC.  |
| EDITOR. Built for real-    | Built for syncing ANY file |
| time collaborative editing | across devices. Not        |
| of a specific document     | designed for real-time     |
| format.                    | collaborative editing.     |
+----------------------------+----------------------------+

Why Dropbox uses conflicted copies:
  Dropbox syncs arbitrary files (.docx, .psd, .zip, etc.).
  It has no understanding of file CONTENT -- it only knows
  that the file changed. Without understanding the content,
  it cannot merge changes. Conflicted copies are the safe
  fallback.

Why Google Docs doesn't need them:
  Google Docs controls the document format AND the editing
  engine. It understands every edit at the character level.
  This enables automatic conflict resolution via OT.
```

### Google Docs vs Git (Three-Way Merge)

```
+----------------------------+----------------------------+
|        GOOGLE DOCS         |          GIT               |
+----------------------------+----------------------------+
| Merge strategy: OT         | Merge strategy: Three-way  |
| transforms in REAL-TIME.   | merge with CONFLICT MARKERS|
| No user intervention.      | (<<<<<<< ======= >>>>>>>). |
|                            | User must manually resolve.|
+----------------------------+----------------------------+
| Collaboration model:       | Collaboration model:       |
| SYNCHRONOUS. All editors   | ASYNCHRONOUS. Developers   |
| see changes in real-time.  | work on branches, merge    |
| No branches, no merges.    | later. Days/weeks between  |
|                            | merge points.              |
+----------------------------+----------------------------+
| Merge granularity:         | Merge granularity:         |
| CHARACTER LEVEL.           | LINE LEVEL.                |
| "Insert X at position 5"  | "Change line 42 from A     |
|                            | to B." If two developers   |
|                            | change the same LINE,      |
|                            | conflict. Different lines  |
|                            | = auto-merge.              |
+----------------------------+----------------------------+
| Conflict frequency: RARE.  | Conflict frequency:        |
| OT resolves most conflicts | COMMON for active repos.   |
| automatically. Users don't | Developers expect and      |
| even know conflicts happen.| handle conflicts regularly. |
+----------------------------+----------------------------+
| Undo: Per-operation,       | Undo: Per-commit (revert). |
| OT-aware. Can undo a       | Reverting a commit undoes  |
| single character insert    | ALL changes in that commit.|
| from 30 minutes ago.       |                            |
+----------------------------+----------------------------+

Why Git uses conflict markers:
  Git is designed for SOFTWARE DEVELOPMENT, where conflicting
  changes to the same line often represent genuine semantic
  conflicts that a human must resolve. Two developers changing
  the same function may have incompatible intentions.

Why Google Docs doesn't need them:
  In a document, two people editing the same paragraph rarely
  have incompatible intentions. Alice adding a sentence and
  Bob fixing a typo nearby are NOT in conflict -- both changes
  should be preserved. OT does this automatically.
```

---

## Interview Tips: What to Emphasize

### L6 Expectations for Conflict Resolution

When discussing conflict resolution in a system design interview, an L6 candidate should:

1. **Trace through specific examples.** Do not hand-wave "OT handles conflicts." Pick two concurrent operations, show the transform step by step, and verify that both clients converge to the same state. The insert-insert at same position with tiebreak is the canonical example.

2. **Explain the client state machine.** Draw the three-state diagram (Synchronized / Awaiting ACK / Awaiting ACK + Buffer). Explain WHY only one in-flight operation (composition in the buffer bounds protocol complexity). Explain what happens when a server operation arrives while in AWAITING ACK + BUFFER state.

3. **Distinguish convergence from intention preservation.** Convergence is mathematical (TP1). Intention preservation is a design goal of the transform functions. They are different properties -- convergence can be proven, intention preservation is a judgment call.

4. **Discuss offline reconnection cost.** The O(M x N) transform cost is a real production concern. Quantify it with specific numbers (500 offline ops x 2000 server ops = 1M transforms). Propose mitigations (composition, batching, limits).

5. **Know at least 5 specific conflict cases.** Insert-insert (same position, tiebreak), delete-delete (overlap), format-format (independent attributes), comment-on-deleted-text (orphan), permission-change-during-editing (reject + downgrade).

### L7 and Beyond

An L7 candidate would additionally discuss:
- OT correctness proofs and testing strategies (property-based testing, fuzzing transform functions, formal verification)
- Intention preservation failures (cases where OT converges correctly but the result surprises users)
- Undo correctness in collaborative editing (transform inverse against all subsequent operations, undo stack per user)
- The relationship between OT convergence and distributed systems consistency models
- Policy decisions that masquerade as algorithmic decisions (same-position insert tiebreak by userId is a policy choice, not a mathematical requirement)

---

*This is a companion document to the main interview simulation. For the full interview dialogue, see [01-interview-simulation.md](01-interview-simulation.md).*
*For the OT engine deep dive, see [03-operational-transformation.md](03-operational-transformation.md).*
*For offline support, see [07-offline-support.md](07-offline-support.md).*
