# Operational Transformation (OT) Deep Dive

> **Companion document to** [01-interview-simulation.md](01-interview-simulation.md)
> **Purpose:** Everything you need to explain OT confidently in a system design interview.

---

## Table of Contents

1. [The Problem](#1-the-problem)
2. [Operations Model](#2-operations-model)
3. [Transform Function](#3-transform-function)
4. [Jupiter Collaboration Protocol](#4-jupiter-collaboration-protocol)
5. [Client-Server Protocol (Google's Model)](#5-client-server-protocol-googles-model)
6. [Server-Side Processing](#6-server-side-processing)
7. [Undo in OT](#7-undo-in-ot)
8. [Comparison: OT vs CRDTs](#8-comparison-ot-vs-crdts)
9. [Complexity Analysis](#9-complexity-analysis)
10. [Key Research Papers](#10-key-research-papers)

---

## 1. The Problem

### Why Naive Application Fails

Two users open the same document simultaneously. Both see:

```
Document (revision 5): "ABCDEFGH"
                         01234567
```

Alice wants to insert `"X"` after position 4 (between `"E"` and `"F"`).
Bob wants to delete the character at position 1 (`"B"`).

Both users generate their operations based on revision 5 of the document. Both send their operations to the server at roughly the same time.

### Naive Application: Apply in Arrival Order Without Transformation

Suppose the server receives Bob's operation first:

```
Server state:     "ABCDEFGH"

Step 1 — Apply Bob's delete(pos=1):
                  "ACDEFGH"       (B is gone, document is now 7 chars)

Step 2 — Apply Alice's insert(pos=5, "X") AS-IS (no transformation):
                  "ACDEFXGH"
                        ^
                        Alice intended X between E and F,
                        but E is now at position 3, not 4.
                        X landed between G and H — WRONG.
```

Now consider what Alice sees locally. She applied her own operation optimistically:

```
Alice's local:    "ABCDEFGH"

Step 1 — Apply her own insert(pos=5, "X"):
                  "ABCDEXFGH"

Step 2 — Apply Bob's delete(pos=1) AS-IS:
                  "ACDEXFGH"
```

And Bob sees:

```
Bob's local:      "ABCDEFGH"

Step 1 — Apply his own delete(pos=1):
                  "ACDEFGH"

Step 2 — Apply Alice's insert(pos=5, "X") AS-IS:
                  "ACDEFXGH"
```

**The result:**

```
Alice sees: "ACDEXFGH"    (X is between E and F — correct intent)
Bob sees:   "ACDEFXGH"    (X is between G and H — wrong intent)
Server has: "ACDEFXGH"    (matches Bob, disagrees with Alice)

THE DOCUMENTS HAVE DIVERGED.
```

This is the fundamental problem. Naive application of concurrent operations produces **different results depending on the order of application**. OT solves this by **transforming** operations to account for the effects of concurrent operations, guaranteeing that all parties converge to the same state and each user's **intent** is preserved.

### What the Correct Result Should Be

Both users' intentions should be preserved:
- Alice intended: insert `"X"` between `"E"` and `"F"`
- Bob intended: delete `"B"`

The correct converged result is: **`"ACDEXFGH"`** — `"B"` is deleted, and `"X"` sits between `"E"` and `"F"`.

---

## 2. Operations Model

### Operations as Document Traversals

In Google Docs' OT model (documented in the Google Wave OT whitepaper by David Wang, 2009), an operation is not a simple "insert at position X." Instead, an operation is a **sequence of components that traverses the entire document from start to end**.

Three component types:

| Component | Meaning | Advances Cursor By |
|---|---|---|
| `retain(n)` | Skip n characters — leave them unchanged | n characters in the input document |
| `insert(text)` | Insert text at the current cursor position | 0 characters in the input (adds to output) |
| `delete(n)` | Delete n characters starting at the current cursor position | n characters in the input document |

### Compound Operations

An operation is a **full traversal**. Every character in the input document must be accounted for — either retained or deleted. Insertions add new characters between existing ones.

**Example 1: Insert `"X"` at position 5 in a 10-character document**

```
Input document:  "ABCDEFGHIJ"   (10 chars)
                  0123456789

Operation: [retain(5), insert("X"), retain(5)]

Execution trace:
  retain(5)    → output "ABCDE",     cursor at input position 5
  insert("X")  → output "ABCDEX",    cursor still at input position 5
  retain(5)    → output "ABCDEXFGHIJ", cursor at input position 10

Result: "ABCDEXFGHIJ"   (11 chars)
```

**Example 2: Delete characters at positions 3 and 4 in a 10-character document**

```
Input document:  "ABCDEFGHIJ"   (10 chars)

Operation: [retain(3), delete(2), retain(5)]

Execution trace:
  retain(3)    → output "ABC",       cursor at input position 3
  delete(2)    → output "ABC",       cursor at input position 5 (skipped D, E)
  retain(5)    → output "ABCFGHIJ",  cursor at input position 10

Result: "ABCFGHIJ"   (8 chars)
```

**Example 3: Replace "DE" with "XYZ" at position 3**

```
Input document:  "ABCDEFGHIJ"   (10 chars)

Operation: [retain(3), delete(2), insert("XYZ"), retain(5)]

Execution trace:
  retain(3)      → output "ABC"
  delete(2)      → skip D, E
  insert("XYZ")  → output "ABCXYZ"
  retain(5)      → output "ABCXYZFGHIJ"

Result: "ABCXYZFGHIJ"   (11 chars)
```

### Operation Validity

An operation is **valid** for a document of length L if and only if:

```
sum of all retain(n) + sum of all delete(n) == L
```

That is, the total number of characters **consumed** from the input (retained + deleted) must equal the document length. If an operation consumes more or fewer characters than the document has, it is invalid and must be rejected.

This invariant is critical — it means every operation unambiguously specifies what happens to every character in the document.

### Operation Composition

Two consecutive operations can be **composed** into a single operation that has the same effect as applying them sequentially.

```
compose(op_A, op_B) → op_AB

Such that:
  apply(apply(doc, op_A), op_B) == apply(doc, op_AB)
```

**Why composition matters:**
- The client buffers multiple local edits while waiting for a server ACK. Instead of growing the buffer unboundedly, each new local edit is **composed** into the existing buffer, keeping it as a single operation.
- Reduces network overhead — send one composed operation instead of many small ones.

**Composition algorithm:** Walk both operations simultaneously. `op_A` produces an intermediate document; `op_B` consumes that intermediate document. The composed operation goes directly from the original document to the final document.

```
Example:
  doc = "ABCDE"  (5 chars)

  op_A = [retain(2), insert("X"), retain(3)]
  After op_A: "ABXCDE"  (6 chars)

  op_B = [retain(4), delete(1), retain(1)]
  After op_B: "ABXCE"  (5 chars)

  compose(op_A, op_B) = [retain(2), insert("X"), retain(1), delete(1), retain(1)]
  Verify: apply("ABCDE", composed) → "ABXCE"  ✓
```

**Composition pseudocode:**

```
function compose(op_A, op_B):
    result = []
    i = 0  // index into op_A components
    j = 0  // index into op_B components

    while i < len(op_A) or j < len(op_B):
        // op_A produces output; op_B consumes that output

        if op_A[i] is insert(s):
            // op_A inserts text — this becomes input for op_B
            if op_B[j] is retain(n):
                // op_B retains the inserted text
                take min(len(s), n) from both
                emit insert(taken_text)
            elif op_B[j] is delete(n):
                // op_B deletes the inserted text — they cancel out
                take min(len(s), n) from both
                // emit nothing (insert + delete = no-op)
            advance as needed

        elif op_A[i] is retain(n):
            // op_A retains — passes through original chars
            if op_B[j] is retain(m):
                emit retain(min(n, m))
            elif op_B[j] is delete(m):
                emit delete(min(n, m))
            elif op_B[j] is insert(s):
                emit insert(s)
                advance j only
                continue
            advance as needed

        elif op_A[i] is delete(n):
            // op_A deletes — these chars never reach op_B
            emit delete(n)
            advance i only

    return result
```

### Operation Inversion

The **inverse** of an operation undoes its effect:

```
invert(op, doc) → op_inverse

Such that:
  apply(apply(doc, op), op_inverse) == doc
```

Inversion rules:
- `retain(n)` inverts to `retain(n)` (no change → no change)
- `insert(text)` inverts to `delete(len(text))` (undo an insertion by deleting it)
- `delete(n)` inverts to `insert(deleted_text)` (undo a deletion by re-inserting the deleted characters)

**Important:** Inverting a `delete` requires knowing **which characters were deleted**. This means the inverse must be computed at the time the operation is applied (when the deleted text is still available), not after the fact.

```
Example:
  doc = "ABCDE"
  op  = [retain(2), delete(2), insert("XY"), retain(1)]
  apply(doc, op) → "ABXYE"

  op_inverse = [retain(2), delete(2), insert("CD"), retain(1)]
  apply("ABXYE", op_inverse) → "ABCDE"  ✓
```

---

## 3. Transform Function

### The Transform Contract

The transform function is the heart of OT. Given two operations `op_A` and `op_B` that were both generated against the **same document state** (i.e., they are concurrent), transform produces modified versions that can be applied in either order to reach the same result:

```
transform(op_A, op_B) → (op_A', op_B')
```

Where:
- `op_A'` is `op_A` adjusted to apply **after** `op_B` has already been applied
- `op_B'` is `op_B` adjusted to apply **after** `op_A` has already been applied

### TP1 (Transformation Property 1): Convergence Guarantee

```
apply(apply(doc, op_A), op_B') == apply(apply(doc, op_B), op_A')
```

Visually as a diamond diagram:

```
                    doc
                   /   \
              op_A/     \op_B
                 /       \
              doc_A     doc_B
                 \       /
             op_B'\     /op_A'
                   \   /
                  doc_final
```

Both paths through the diamond must reach the same `doc_final`. This is the **convergence guarantee** — the property that makes collaborative editing work.

### Transform Cases: The Complete Matrix

The transform algorithm walks both operations component by component, consuming input from both sides. Here is every case that arises:

#### Case 1: retain x retain

Both operations skip the same characters. Neither modifies anything. Emit retain in both outputs.

```
op_A: retain(5)     op_B: retain(5)

op_A': retain(5)    op_B': retain(5)
```

If the retain lengths differ, consume the minimum and continue:

```
op_A: retain(3)     op_B: retain(5)

Consume min(3, 5) = 3:
  op_A': retain(3)    op_B': retain(3)
  Remaining: op_A has 0 left, op_B has retain(2) left → continue
```

#### Case 2: retain x insert

`op_B` inserts text. `op_A` is just retaining. Since `op_B`'s insertion adds new characters, `op_A'` must retain over those new characters.

```
op_A: retain(5)           op_B: insert("XY")

op_A': retain(2) + ...    op_B': insert("XY")
       ^^^^^^^^
       retain over the 2 newly inserted chars
```

The inserted text from `op_B` produces 2 new characters. `op_A'` emits `retain(2)` to skip over them. `op_B'` emits the insert unchanged (since `op_A`'s retain doesn't affect anything).

Note: The retain from `op_A` is NOT consumed by `op_B`'s insert — inserts don't consume input characters.

#### Case 3: retain x delete

`op_B` deletes characters. `op_A` retains over them. Since `op_B` already deleted those characters, `op_A'` has nothing to retain — those characters are gone.

```
op_A: retain(5)     op_B: delete(3)

Consume min(5, 3) = 3:
  op_A': (nothing — the 3 retained chars were deleted)
  op_B': delete(3)

Remaining: op_A has retain(2) left → continue
```

`op_A'` skips over the deleted characters (emits nothing for them). `op_B'` emits the delete unchanged.

#### Case 4: insert x insert (different positions)

Both operations insert text. Since inserts don't consume input, we need a tiebreaking rule to decide which insert goes first.

If `op_A` and `op_B` are at different positions in their document traversal, the one that is "earlier" in the document naturally goes first — handled by the retain components before the inserts.

But when **both inserts occur at the same position** (i.e., we're processing an insert from `op_A` and an insert from `op_B` simultaneously), we need an explicit tiebreak.

#### Case 5: insert x insert (same position — tiebreak)

```
op_A: insert("X")     op_B: insert("Y")

Tiebreak by client ID (or any deterministic rule — e.g., lower ID goes first):

If A wins the tiebreak:
  op_A': insert("X")        op_B': retain(1), insert("Y")
                                    ^^^^^^^^^
                                    skip over A's inserted "X", then insert "Y"

If B wins the tiebreak:
  op_A': retain(1), insert("X")    op_B': insert("Y")
```

**The tiebreak must be deterministic and consistent.** Google Docs uses client/user ID for tiebreaking — the client with the lower ID "wins" and its insert goes first. This ensures all clients agree on the order.

**Full example:**

```
doc = "ABCDE"  (5 chars)

op_A = [retain(3), insert("X"), retain(2)]   — insert X at pos 3
op_B = [retain(3), insert("Y"), retain(2)]   — insert Y at pos 3

Assume A wins tiebreak:

transform(op_A, op_B):
  Walk both ops:
    retain(3) x retain(3) → A': retain(3), B': retain(3)
    insert("X") x insert("Y") → A wins →
        A': insert("X")
        B': retain(1)     (skip over A's "X")
    (Now B still has insert("Y") pending)
        A': retain(1)     (skip over B's "Y")
        B': insert("Y")
    retain(2) x retain(2) → A': retain(2), B': retain(2)

Final:
  op_A' = [retain(3), insert("X"), retain(1), retain(2)]
        = [retain(3), insert("X"), retain(3)]

  op_B' = [retain(3), retain(1), insert("Y"), retain(2)]
        = [retain(4), insert("Y"), retain(2)]

Verify:
  Path 1: doc → op_A → op_B'
    "ABCDE" → [r(3), i("X"), r(2)] → "ABCXDE"
    "ABCXDE" → [r(4), i("Y"), r(2)] → "ABCXYDE"  ✓

  Path 2: doc → op_B → op_A'
    "ABCDE" → [r(3), i("Y"), r(2)] → "ABCYDE"
    "ABCYDE" → [r(3), i("X"), r(3)] → "ABCXYDE"  ✓

Both paths converge to "ABCXYDE"  ✓
```

#### Case 6: insert x delete

`op_A` inserts text. `op_B` deletes characters. The insert happens "before" the deletion point from `op_B`'s perspective. `op_B'` must account for the newly inserted characters.

```
op_A: insert("XY")     op_B: delete(3)

op_A': insert("XY")                 (insert is unchanged)
op_B': retain(2), delete(3)         (skip over 2 new chars, then delete)
       ^^^^^^^^^
       skip the inserted "XY" before proceeding with the delete
```

The insert from `op_A` doesn't consume any input, so `op_B`'s delete still applies to the same original characters. But in `op_B'`, those characters have shifted right by 2 (the length of the inserted text), so `op_B'` must retain(2) first.

#### Case 7: delete x insert

Symmetric to Case 6.

```
op_A: delete(3)     op_B: insert("XY")

op_A': retain(2), delete(3)    (skip B's inserted "XY", then delete)
op_B': insert("XY")            (insert is unchanged)
```

#### Case 8: delete x delete (same character)

Both operations delete the same character(s). Since the character is already gone after the first operation, the second becomes a no-op for those characters.

```
op_A: delete(3)     op_B: delete(3)

(Both delete the same 3 characters)

op_A': (nothing — those chars are already deleted by B)
op_B': (nothing — those chars are already deleted by A)
```

In the transform output, we simply skip those characters in both `op_A'` and `op_B'`.

#### Case 9: delete x delete (different characters / partial overlap)

```
doc = "ABCDEFGH"  (8 chars)

op_A = [retain(2), delete(3), retain(3)]     — delete "CDE"
op_B = [retain(4), delete(3), retain(1)]     — delete "EFG"

Walk the transform:
  retain(2) x retain(2): trivial → A': retain(2), B': retain(2)
      (remaining: A has delete(3), B has retain(2))

  delete(2) x retain(2): A deletes chars that B retains
      consume min(2, 2) = 2
      A': delete(2)     (A deletes 2 chars that still exist after B)
      B': (nothing)     (those 2 chars are deleted by A; B was retaining them)
      (remaining: A has delete(1), B has nothing at this point)

  Now B advances to its next component: delete(3)
  delete(1) x delete(1): A and B both delete the same char (E, at original pos 4)
      consume min(1, 1) = 1
      A': (nothing)     (char already deleted by B)
      B': (nothing)     (char already deleted by A)
      (remaining: A done with delete, B has delete(2))

  A advances: retain(3)
  retain(2) x delete(2): B deletes chars that A retains
      consume min(2, 2) = 2
      A': (nothing)     (those 2 chars are deleted by B; A was retaining them)
      B': delete(2)     (B deletes 2 chars that still exist after A)
      (remaining: A has retain(1), B done)

  retain(1) x (end): A retains the last char
      A': retain(1)
      B': retain(1)

Final:
  op_A' = [retain(2), delete(2), retain(1)]
  op_B' = [retain(2), delete(2), retain(1)]

Verify:
  Path 1: "ABCDEFGH" → op_A → "ABFGH" → op_B' → "ABH"
    op_B' = [retain(2), delete(2), retain(1)]
    "ABFGH" → skip A,B → delete F,G → keep H → "ABH"  ✓

  Path 2: "ABCDEFGH" → op_B → "ABCDH" → op_A' → "ABH"
    op_A' = [retain(2), delete(2), retain(1)]
    "ABCDH" → skip A,B → delete C,D → keep H → "ABH"  ✓

Both paths converge to "ABH"  ✓
```

#### Case 10: format x insert

A format operation applies attributes to a range of characters. When the other operation inserts text within that range, the format range may need to expand.

```
doc = "ABCDEFGH"  (8 chars)

op_A = [retain(2), format(4, {bold: true}), retain(2)]
       — bold characters at positions 2-5 ("CDEF")

op_B = [retain(4), insert("XY"), retain(4)]
       — insert "XY" at position 4 (between D and E)

Transform:
  op_A' = [retain(2), format(6, {bold: true}), retain(2)]
          — bold range expands from 4 to 6 (the inserted "XY" falls within
            the bolded region, so they become bold too)

  op_B' = [retain(4), insert("XY"), retain(4)]
          — unchanged (format doesn't shift positions or change text)

Verify:
  Path 1: doc → op_A → "ABCDEFGH" (C-F bolded) → op_B' → "ABCDXYEFGH" (C-F,X,Y bolded)
  Path 2: doc → op_B → "ABCDXYEFGH" → op_A' → "ABCDXYEFGH" (C-F,X,Y bolded)  ✓
```

**Design choice:** Should inserted characters inherit the format? In Google Docs, if you insert text in the middle of a bold region, the inserted text is bold. The format operation's transformed range expands to include the insertion. However, if the insertion is at the **boundary** of the format range, the behavior depends on the product decision (Google Docs typically does NOT extend bold to text typed at the end of a bold run unless the cursor was placed inside the bold text).

#### Case 11: format x delete

A format operation targets a range. The other operation deletes some of the characters in that range. The format range shrinks.

```
doc = "ABCDEFGH"  (8 chars)

op_A = [retain(2), format(4, {bold: true}), retain(2)]
       — bold "CDEF"

op_B = [retain(3), delete(2), retain(3)]
       — delete "DE"

Transform:
  op_A' = [retain(2), format(2, {bold: true}), retain(2)]
          — bold range shrinks from 4 to 2 (D and E were deleted, only C and F remain)

  op_B' = [retain(3), delete(2), retain(3)]
          — unchanged (format doesn't affect positions)
```

If the deletion covers the **entire** format range, the format operation becomes a no-op (there is nothing left to format).

#### Case 12: format x format

Two concurrent format operations on the same range. The result depends on whether they modify the **same** attribute or **different** attributes.

**Different attributes — no conflict:**

```
op_A = format(range, {bold: true})
op_B = format(range, {italic: true})

op_A' = format(range, {bold: true})     — unchanged
op_B' = format(range, {italic: true})   — unchanged

Result: text is both bold AND italic.
```

**Same attribute — conflict requires tiebreak:**

```
op_A = format(range, {color: "red"})
op_B = format(range, {color: "blue"})

Tiebreak: last-writer-wins, where "last" is determined by client ID or server arrival order.

If A wins:  result color = red
If B wins:  result color = blue
```

**Overlapping ranges with the same attribute:**

```
doc = "ABCDEFGH"

op_A = format(pos 2-5, {bold: true})    — bold "CDEF"
op_B = format(pos 4-7, {bold: false})   — unbold "EFGH"

The overlapping region (pos 4-5, "EF") has conflicting bold values.
Tiebreak determines the result for the overlap.
Non-overlapping regions are unambiguous:
  C, D: bold (only A touches them)
  G, H: unbold (only B touches them)
  E, F: tiebreak decides
```

### Transform Algorithm: Pseudocode

The complete transform function walks both operations component by component:

```
function transform(op_A, op_B):
    a_prime = []    // transformed op_A (to apply after op_B)
    b_prime = []    // transformed op_B (to apply after op_A)

    i = 0, j = 0   // component indices
    // Track remaining length for partially consumed components
    a_remaining = 0, b_remaining = 0

    while i < len(op_A) or j < len(op_B):
        a_comp = current_component(op_A, i, a_remaining)
        b_comp = current_component(op_B, j, b_remaining)

        // ─── Case: A inserts ─────────────────────────────
        if a_comp is insert(s):
            a_prime.append(insert(s))     // A's insert passes through
            b_prime.append(retain(len(s)))  // B must skip over A's insert
            advance(i, a_remaining)       // consume A's component
            // do NOT advance j — inserts don't consume input
            continue

        // ─── Case: B inserts ─────────────────────────────
        if b_comp is insert(s):
            a_prime.append(retain(len(s)))  // A must skip over B's insert
            b_prime.append(insert(s))     // B's insert passes through
            advance(j, b_remaining)
            continue

        // ─── Case: A inserts AND B inserts (same position) ───
        // (Handled above — whichever insert is processed first
        //  depends on tiebreak. By convention, if A's client ID
        //  is lower, process A's insert first.)

        // ─── From here, both A and B consume input characters ─

        // ─── Case: retain x retain ──────────────────────
        if a_comp is retain(n) and b_comp is retain(m):
            min_len = min(n, m)
            a_prime.append(retain(min_len))
            b_prime.append(retain(min_len))
            consume(n, m, min_len, ...)

        // ─── Case: retain x delete ──────────────────────
        elif a_comp is retain(n) and b_comp is delete(m):
            min_len = min(n, m)
            // B deleted these chars. A was retaining them.
            // In A', we skip them (they're gone). B' keeps the delete.
            // a_prime: nothing (chars no longer exist)
            b_prime.append(delete(min_len))
            consume(n, m, min_len, ...)

        // ─── Case: delete x retain ──────────────────────
        elif a_comp is delete(n) and b_comp is retain(m):
            min_len = min(n, m)
            a_prime.append(delete(min_len))
            // b_prime: nothing (chars deleted by A)
            consume(n, m, min_len, ...)

        // ─── Case: delete x delete ──────────────────────
        elif a_comp is delete(n) and b_comp is delete(m):
            min_len = min(n, m)
            // Both delete the same chars — they cancel out.
            // Neither A' nor B' emits anything for these chars.
            consume(n, m, min_len, ...)

    return (compact(a_prime), compact(b_prime))
```

The `compact` function merges adjacent components of the same type (e.g., `retain(3), retain(2)` becomes `retain(5)`).

**Handling insert-insert tiebreak in the algorithm:**

```
// When both a_comp and b_comp are inserts at the same position:
if a_comp is insert and b_comp is insert:
    if client_id_A < client_id_B:
        // Process A's insert first
        a_prime.append(insert(a_comp.text))
        b_prime.append(retain(len(a_comp.text)))
        advance A
    else:
        // Process B's insert first
        a_prime.append(retain(len(b_comp.text)))
        b_prime.append(insert(b_comp.text))
        advance B
```

---

## 4. Jupiter Collaboration Protocol

### Origin

The Jupiter collaboration protocol was published by Nichols, Curtis, Dixon, and Lamping in 1995 at UIST (ACM Symposium on User Interface Software and Technology). It was developed at Xerox PARC as part of the Jupiter multimedia virtual world project.

**Paper:** *High-Latency, Low-Bandwidth Windowing in the Jupiter Collaboration System* (Nichols et al., UIST 1995)

### Client-Server Architecture

Jupiter's key architectural insight is: **use a client-server model instead of peer-to-peer.** Each client communicates only with a central server; clients never communicate directly with each other.

```
        Client A ◄──────► Server ◄──────► Client B
        Client C ◄──────►   │
                             │
                         (total ordering)
```

This simplifies the OT problem enormously compared to peer-to-peer approaches (like dOPT by Ellis & Gibbs, 1989).

### The 2D State Space Graph

Jupiter models the collaboration between each client-server pair as a **2D state space**. One axis represents operations generated by the client; the other represents operations generated by the server (i.e., operations from other clients that the server has processed).

```
server ops
    ▲
    │
  3 │    ·────·────·────·
    │    │    │    │    │
  2 │    ·────·────·────·
    │    │    │    │    │
  1 │    ·────S────·────·
    │    │  ↗ │  ↗ │    │
  0 │    C────·────·────·
    └──────────────────────► client ops
         0    1    2    3

C = initial state for client
S = initial state for server
```

When the client generates an operation, it moves right (along the client axis). When the server processes an operation from another client, it moves up (along the server axis). The client and server may be at different positions in this 2D space.

**The goal:** Both client and server must reach the same final state — they must converge to the same point in the state space. OT transform is the function that "goes around a corner" in this graph:

```
    · ─── · (server applied op_S, then needs to apply transformed op_C')
    |     |
    |     |
    · ─── · (client applied op_C, then needs to apply transformed op_S')

transform(op_C, op_S) → (op_C', op_S')
```

### Why Jupiter Only Needs TP1

In a client-server model, the server processes operations **one at a time, in order**. For each client-server pair, the state space is a simple 2D grid, and operations only need to be transformed **pairwise** — operation A against operation B.

TP1 (Transformation Property 1) guarantees that one such pairwise transform produces convergence:

```
apply(apply(doc, A), B') == apply(apply(doc, B), A')
```

This is sufficient because the server imposes a **total order**. There is never a situation where three or more operations must be transformed simultaneously in a way that requires a different property.

### TP2 and Why It's Hard

**TP2 (Transformation Property 2)** is required for **peer-to-peer** OT, where there is no central server imposing a total order. TP2 guarantees that transforming an operation against two concurrent operations in **different orders** produces the same result:

```
TP2:
  Given three concurrent operations: op_A, op_B, op_C

  Path 1: transform op_C against op_A first, then against transformed op_B'
  Path 2: transform op_C against op_B first, then against transformed op_A'

  TP2 requires: both paths produce the same result for op_C
```

Visually, TP2 requires that all paths through a **3D state space cube** converge:

```
         ·─────────·
        /|         /|
       / |        / |
      ·─────────·  |
      |  ·──────|──·
      | /       | /
      |/        |/
      ·─────────·

All paths from one corner to the opposite corner must produce the same result.
```

**Why TP2 is hard:**

1. **Combinatorial complexity:** With 3+ concurrent operations, the number of paths through the state space grows factorially. Each path must produce the same result.

2. **Many published algorithms got TP2 wrong.** Imine, Molli, Oster, and Rusinowitch (2003) formally analyzed published OT algorithms and found that several violated TP2:
   - The adOPTed algorithm (Ressel et al., 1996) was designed specifically to satisfy TP2, but the proof was complex and has been questioned.
   - Sun & Ellis's GOT/GOTO algorithm (1998) used history buffers and transformation paths, but correctness was difficult to verify.
   - Multiple other algorithms claimed TP2 compliance but had subtle bugs.

3. **The academic consensus** (as of the mid-2000s) was that getting TP2 right for arbitrary operation types is extremely difficult, and any implementation must be rigorously formally verified.

**Jupiter's insight:** By using a client-server model, you sidestep TP2 entirely. The server is the single serialization point. Each client only needs pairwise transforms against the server's operation sequence. TP1 is sufficient, and TP1 is much easier to implement correctly.

This is why Google chose the Jupiter-based approach for Google Docs — correctness is achievable with TP1 alone.

---

## 5. Client-Server Protocol (Google's Model)

Google Docs uses a variant of the Jupiter protocol, documented in the Google Wave OT whitepaper (David Wang, 2009). The protocol is built around a **three-state client model**.

### Three-State Client Model

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│                      ┌───────────────────┐                          │
│              ┌──────►│   SYNCHRONIZED    │◄─────────┐               │
│              │       │                   │          │               │
│              │       │ - No pending ops  │          │               │
│              │       │ - Client = Server │          │               │
│              │       └─────────┬─────────┘          │               │
│              │                 │                     │               │
│              │      User edits │locally              │               │
│              │      Send op to │server               │               │
│     Server ACKs,               │                     │               │
│     no buffer        ┌────────▼──────────┐          │               │
│              │       │   AWAITING ACK    │          │               │
│              └───────│                   │          │               │
│                      │ - 1 op in-flight  │   Server ACKs,           │
│                      │   (sent, not      │   send buffer,           │
│                      │    ack'd yet)     │   enter AWAITING ACK     │
│                      │ - No buffer       │          │               │
│                      └─────────┬─────────┘          │               │
│                                │                     │               │
│                     User edits │locally               │               │
│                     Buffer the │edit                  │               │
│                     (compose into │buffer)             │               │
│                                │                     │               │
│                      ┌─────────▼─────────┐          │               │
│                      │   AWAITING ACK    │          │               │
│                      │     + BUFFER      ├──────────┘               │
│                      │                   │                          │
│                      │ - 1 op in-flight  │                          │
│                      │ - 1 op buffered   │                          │
│                      │ - New edits       │                          │
│                      │   compose into    │                          │
│                      │   buffer          │                          │
│                      └───────────────────┘                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### State Transitions

**Transition 1: SYNCHRONIZED -> AWAITING ACK**
```
Trigger: User makes a local edit.
Action:
  1. Apply the operation to the local document immediately (optimistic).
  2. Send the operation to the server (tagged with the current revision number).
  3. Enter AWAITING ACK state. The sent operation is the "in-flight" op.
```

**Transition 2: AWAITING ACK -> AWAITING ACK + BUFFER**
```
Trigger: User makes another local edit while waiting for the ACK.
Action:
  1. Apply the operation to the local document immediately.
  2. Store the operation in the buffer (do NOT send it yet).
  3. If the buffer already has an operation, compose the new edit into it:
     buffer = compose(buffer, new_edit)
     This keeps the buffer as a single operation, not a growing list.
```

**Transition 3: AWAITING ACK + BUFFER -> AWAITING ACK**
```
Trigger: Server ACKs the in-flight operation.
Action:
  1. Discard the in-flight operation (it's been acknowledged).
  2. Send the buffered operation to the server as the new in-flight op.
  3. Clear the buffer.
  4. Enter AWAITING ACK state.
```

**Transition 4: AWAITING ACK -> SYNCHRONIZED**
```
Trigger: Server ACKs the in-flight operation, and there is no buffer.
Action:
  1. Discard the in-flight operation.
  2. Enter SYNCHRONIZED state.
```

### Handling Server Operations in Each State

When the server sends an operation from another user (a "server op"), the client must handle it differently depending on its current state.

**In SYNCHRONIZED state:**

```
Server sends: op_S

Action:
  1. Apply op_S directly to the local document.
  2. No transformation needed — the client is in sync.
```

**In AWAITING ACK state:**

```
Client has: in_flight = op_C (sent but not yet ACK'd)
Server sends: op_S

  The server has already transformed op_S against our in-flight op_C
  (because the server processes operations sequentially).

  But WE need to transform our view of things:

  transform(op_C, op_S) → (op_C', op_S')

  Action:
  1. Replace in_flight with op_C'
     (our op, as the server will see it after applying op_S)
  2. Apply op_S' to the local document
     (the server op, adjusted for our local in-flight op)
```

**In AWAITING ACK + BUFFER state:**

```
Client has: in_flight = op_C, buffer = op_B
Server sends: op_S

  Step 1: Transform in-flight against server op
    transform(op_C, op_S) → (op_C', op_S')

  Step 2: Transform buffer against the (already once-transformed) server op
    transform(op_B, op_S') → (op_B', op_S'')

  Action:
  1. Replace in_flight with op_C'
  2. Replace buffer with op_B'
  3. Apply op_S'' to the local document
```

This chain of transforms ensures that the local document state remains consistent with what the server will eventually produce.

### Visual Example: Full Protocol Flow

```
Time ──────────────────────────────────────────────────────────────►

Client A                    Server                    Client B
(Alice)                                               (Bob)
  │                           │                          │
  │  State: SYNCHRONIZED      │   doc rev: 10            │
  │  doc: "HELLO"             │   doc: "HELLO"           │
  │                           │                          │
  │  Alice types "X" at pos 2 │                          │
  │  Local: "HEXLLO"          │                          │
  │  Send: op_A [r(2),i("X"),r(3)] rev=10               │
  │  State: AWAITING ACK      │                          │
  │ ─────────────────────────►│                          │
  │                           │                          │
  │                           │   Bob sends: op_B [r(4),i("Y"),r(1)] rev=10
  │                           │◄──────────────────────────│
  │                           │                          │
  │                           │   Server receives op_B first (at rev 10)
  │                           │   Apply op_B → "HELLYО" rev=11
  │                           │
  │                           │   Broadcast op_B to Alice
  │ ◄─────────────────────────│
  │                           │
  │  Receive op_B from server │
  │  State: AWAITING ACK      │
  │  in_flight = op_A         │
  │                           │
  │  transform(op_A, op_B):   │
  │    op_A = [r(2),i("X"),r(3)]
  │    op_B = [r(4),i("Y"),r(1)]
  │                           │
  │    op_A' = [r(2),i("X"),r(4)]  (retain grows by 1 for Y)
  │    op_B' = [r(4),i("Y"),r(2)]  (retain grows by 1 for X... but
  │                                  wait — X was at pos 2, Y at pos 4.
  │                                  X is BEFORE Y, so Y's position
  │                                  shifts: op_B' = [r(5),i("Y"),r(1)])
  │                           │
  │  Actually, let me redo this carefully:
  │    op_A inserts at pos 2. op_B inserts at pos 4.
  │    A's insert is before B's. So after A, B's position shifts +1.
  │    After B, A's position doesn't shift (B is after A).
  │                           │
  │    op_A' = [r(2),i("X"),r(4)]    (extra retain for Y at end)
  │    op_B' = [r(5),i("Y"),r(1)]    (pos 4→5 because of X)
  │                           │
  │  But we apply op_B' to our local doc:
  │  Local was: "HEXLLO" (after our local op_A)
  │  Apply op_B': [r(5),i("Y"),r(1)]
  │  "HEXLLO" → "HEXLLYО"    │
  │                           │
  │  Replace in_flight = op_A'│
  │                           │
  │                           │   Server receives op_A (rev=10)
  │                           │   Server is at rev 11. Must transform:
  │                           │   transform(op_A, op_B):
  │                           │     op_A' = [r(2),i("X"),r(4)]
  │                           │   Apply op_A' → "HEXLLYО" rev=12
  │                           │
  │                           │   Broadcast op_A' to Bob:
  │                           │ ─────────────────────────►│
  │                           │                          │
  │   ACK (rev=12)            │                          │
  │ ◄─────────────────────────│                          │
  │                           │                          │ Apply op_A'
  │  State: SYNCHRONIZED      │                          │ to "HELLYО":
  │  doc: "HEXLLYО"           │  doc: "HEXLLYО"          │ "HEXLLYО"
  │                           │                          │
  │  ALL THREE CONVERGE  ✓    │                          │
```

### Operation Buffer and Composition

Why limit to **one in-flight + one buffer** instead of sending every operation immediately?

1. **Simplicity:** With one in-flight op, the server knows exactly which operation to ACK. With multiple in-flight ops, tracking which have been received, which transformed, and which ACK'd becomes complex.

2. **Composition keeps the buffer compact:** If Alice types 10 characters while waiting for an ACK, those 10 operations are composed into a single operation in the buffer. When the ACK arrives, one composed operation is sent — not 10 separate operations.

3. **Reduced transform cost:** The server only needs to transform one incoming operation at a time, not a batch.

```
Example: Composing buffer edits

Buffer starts empty.
Alice types "A" at pos 0:  buffer = [insert("A"), retain(5)]
Alice types "B" at pos 1:  compose with buffer →
                           buffer = [insert("AB"), retain(5)]
Alice types "C" at pos 2:  compose with buffer →
                           buffer = [insert("ABC"), retain(5)]

When ACK arrives, send single op: [insert("ABC"), retain(5)]
Instead of 3 separate operations.
```

---

## 6. Server-Side Processing

### Server as Single Source of Truth

The server maintains the **canonical document state**. All clients' states are derived from the server's state plus any local, unacknowledged operations. If there is ever a disagreement, the server wins.

This is the core advantage of centralized OT over peer-to-peer approaches: there is always one definitive version of the document.

### Total Ordering of Operations

The server assigns a **monotonically increasing revision number** to each operation it applies. This creates a total order:

```
rev 1: op from Alice
rev 2: op from Bob
rev 3: op from Alice
rev 4: op from Carol
rev 5: op from Bob
...
```

Every client will eventually see every operation, in this exact order. Even if operations arrived at the server in a different order than they were generated, the server's total ordering is the canonical sequence.

### Server Processing Pipeline

When the server receives an operation from a client:

```
function server_receive(client_op, base_revision, client_id):
    // 1. Validate: is the base_revision reasonable?
    if base_revision > server_revision:
        reject("base revision is in the future")
    if base_revision < earliest_available_revision:
        reject("base revision too old — client needs to reload")

    // 2. Transform against all operations since base_revision
    transformed_op = client_op
    for each server_op in operation_log[base_revision + 1 .. server_revision]:
        (transformed_op, _) = transform(transformed_op, server_op)

    // 3. Validate the transformed operation
    if not is_valid(transformed_op, current_document):
        reject("invalid operation after transformation")

    // 4. Apply to canonical document state
    apply(current_document, transformed_op)

    // 5. Append to operation log
    server_revision += 1
    operation_log.append(server_revision, transformed_op, client_id, timestamp)

    // 6. ACK the originating client
    send_ack(client_id, server_revision)

    // 7. Broadcast to all OTHER connected clients
    for each other_client in connected_clients:
        if other_client != client_id:
            send_operation(other_client, transformed_op, server_revision)
```

### Operation Log (Append-Only)

The operation log is an **append-only, immutable** sequence:

```
┌─────────┬───────────┬─────────────────────────────┬──────────────────────┐
│ Rev     │ Client ID │ Transformed Operation        │ Timestamp            │
├─────────┼───────────┼─────────────────────────────┼──────────────────────┤
│ 1       │ alice     │ [insert("Hello")]            │ 2026-02-21T10:00:00  │
│ 2       │ bob       │ [retain(5), insert(" World")]│ 2026-02-21T10:00:01  │
│ 3       │ alice     │ [retain(11), insert("!")]    │ 2026-02-21T10:00:02  │
│ 4       │ carol     │ [delete(5), retain(7)]       │ 2026-02-21T10:00:03  │
│ ...     │ ...       │ ...                          │ ...                  │
└─────────┴───────────┴─────────────────────────────┴──────────────────────┘
```

**Key properties:**
- **Immutable:** Once written, an operation is never modified or deleted.
- **Ordered:** Revisions are strictly sequential.
- **Source of truth:** The document at any revision = replay operations 1 through that revision (or load a snapshot and replay from there).
- **Storage:** In Google's infrastructure, this is likely stored in Bigtable or Spanner, keyed by `(document_id, revision)` for efficient sequential reads.

### Transform Against All Operations Since Client's Revision

When a client sends an operation based on revision `r`, and the server is at revision `r + n`, the server must transform the incoming operation against **n** server operations:

```
Client sends: op_C based on rev 5
Server is at: rev 8

Server must transform op_C against:
  rev 6: op_X
  rev 7: op_Y
  rev 8: op_Z

Step 1: transform(op_C, op_X) → (op_C1, _)
Step 2: transform(op_C1, op_Y) → (op_C2, _)
Step 3: transform(op_C2, op_Z) → (op_C3, _)

Apply op_C3 to server document. This becomes rev 9.
```

This is an O(n) chain of transforms, where n is the number of operations the client has "missed." For a responsive system, n should be small (client sends operations frequently). For offline reconnection, n can be very large — see Section 7.

### Broadcast to All Other Clients

After applying the transformed operation, the server broadcasts it to all connected clients (except the originator, who receives an ACK instead).

Each receiving client transforms the broadcast operation against their own local pending operations (in-flight + buffer), as described in Section 5.

---

## 7. Undo in OT

### Why Undo Is Non-Trivial

In a single-user editor, undo is simple: pop the last operation from the history stack and apply its inverse. But in a collaborative editor, **other users' operations have interleaved** since the operation you want to undo.

**Example showing why naive undo fails:**

```
Time   User    Operation                  Document State
────   ────    ─────────                  ──────────────
t1     Alice   insert("ABC") at pos 0     "ABC"
t2     Bob     insert("XY") at pos 1      "AXYBC"
t3     Alice   wants to undo her t1 op

Naive undo: inverse of insert("ABC") at pos 0 = delete(3) at pos 0

Apply delete(3) at pos 0 to "AXYBC":
  Delete first 3 characters → "BC"

But the correct result should be: "XY"
(Alice's "ABC" is undone, but Bob's "XY" should survive)
```

The naive undo deleted `"AXY"` instead of `"ABC"` because Bob's insertion at pos 1 interleaved with Alice's original text. The characters at positions 0-2 are no longer `"ABC"` — they are `"AXY"`.

### OT-Aware Undo

The correct approach:

1. Compute the **inverse** of the operation to be undone (at the time it was originally applied).
2. **Transform** the inverse against all operations that have been applied since the original operation.
3. Apply the transformed inverse.

```
Step 1: Inverse of Alice's t1 operation
  Original: insert("ABC") at pos 0 → [insert("ABC"), retain(0)]
  Inverse: delete(3) at pos 0 → [delete(3)]
  (This inverse is valid for the document state at t1: "ABC")

Step 2: Transform inverse against all subsequent operations

  Subsequent ops:
    t2: Bob's insert("XY") at pos 1 → [retain(1), insert("XY"), retain(2)]

  transform(delete(3), Bob's_insert):
    Walk: delete(1) x retain(1) → delete(1) in A', nothing in B'
    Walk: (remaining delete(2)) x insert("XY") → retain(2) in A' (skip XY), insert stays
    Walk: (remaining delete(2)) x retain(2) → delete(2) in A'

    Transformed inverse: [delete(1), retain(2), delete(2)]

Step 3: Apply transformed inverse to current doc "AXYBC"
  delete(1): delete "A" → "XYBC"
  retain(2): keep "XY" → "XY..."
  delete(2): delete "BC" → "XY"

Final result: "XY"  ✓  (Alice's ABC is gone, Bob's XY survives)
```

### Undo Stack in Collaborative Editing

Each client maintains its own local undo stack containing only **its own operations**. When the user presses Ctrl+Z:

1. Pop the most recent own-operation from the undo stack.
2. Compute its inverse.
3. Transform the inverse against all operations (from all users) that occurred after the original.
4. Send the transformed inverse to the server as a new operation.

The transformed inverse is a regular operation from the server's perspective — it goes through the normal OT pipeline.

**Important:** The undo is **intention-preserving** — it undoes the user's own action while respecting everything that happened afterward. It does NOT undo other users' work.

---

## 8. Comparison: OT vs CRDTs

### CRDTs: Conflict-free Replicated Data Types

CRDTs take a fundamentally different approach to concurrent editing. Instead of transforming operations to resolve conflicts, CRDTs design the **data structure itself** so that concurrent operations **commute mathematically** — applying operations in any order always produces the same result, with no transformation needed.

#### RGA (Replicated Growable Array)

**Paper:** Roh, Jeon, Kim, and Lee (2011)

RGA assigns a **globally unique ID** to every character. IDs are typically `(siteId, logicalClock)` tuples. When inserting, you reference the ID of the character your new character is inserted **after**.

```
RGA representation of "HELLO":

  ID         Char   After
  ────       ────   ─────
  (A, 1)     H      root
  (A, 2)     E      (A, 1)
  (A, 3)     L      (A, 2)
  (A, 4)     L      (A, 3)
  (A, 5)     O      (A, 4)
```

**Deletion** marks characters as **tombstones** (logically deleted but not physically removed). Tombstones are needed so that concurrent insertions that reference the deleted character's ID can still be resolved.

```
Delete 'L' at position 2:

  ID         Char   After     Tombstone?
  ────       ────   ─────     ──────────
  (A, 1)     H      root      no
  (A, 2)     E      (A, 1)    no
  (A, 3)     L      (A, 2)    YES (deleted)
  (A, 4)     L      (A, 3)    no
  (A, 5)     O      (A, 4)    no

Visible text: "HELO"
But the tombstone for (A,3) remains in the data structure forever.
```

**Pros:** No central server needed. Operations commute by construction. Peer-to-peer works natively.

**Cons:** Tombstones grow unboundedly. Every character ever inserted (even if deleted) consumes memory. Garbage collecting tombstones safely requires consensus among all replicas.

#### YATA (Yet Another Transformation Approach) — Yjs

**Library:** Yjs (by Kevin Jahns, 2015+)

YATA is the algorithm behind Yjs, one of the most widely used CRDT libraries for collaborative editing. Each character is stored as a node in a **doubly-linked list** with references to its left and right neighbors (by ID).

```
YATA item structure:
  {
    id: (clientId, clock),
    content: "a",
    left: (ref to left neighbor's ID),
    right: (ref to right neighbor's ID),
    deleted: false
  }
```

When concurrent insertions happen at the same position, YATA uses a deterministic rule based on the left/right neighbor IDs and creator IDs to decide ordering. This avoids the need for a central tiebreaker.

**Yjs in practice:**
- Used by many open-source collaborative editors
- Very efficient implementation in JavaScript
- Supports text, rich text (via Yjs's `Y.XmlFragment`), and arbitrary JSON structures
- Sub-document support for large documents (split into independently-syncing chunks)

#### Automerge

**Library:** Automerge (by Martin Kleppmann et al., 2017+)

Automerge is an RGA-based CRDT library with a Rust core for performance. It provides a JSON-like document model where any field can be collaboratively edited.

- **Text editing:** Uses RGA under the hood. Each character has a unique ID.
- **Rich text:** Supported via mark/unmark operations on character ranges.
- **Storage:** Compact binary format. Supports incremental saves (only new operations, not the full document).
- **Sync protocol:** Efficient sync based on Bloom filters to determine which operations each peer is missing.

### Detailed Comparison Table

| Dimension | OT (Google Docs) | CRDTs (RGA / YATA / Automerge) |
|---|---|---|
| **Central server required?** | Yes — server imposes total order | No — peers can sync directly |
| **Convergence mechanism** | Transform function adjusts concurrent ops | Data structure ensures commutativity by construction |
| **Correctness** | Must prove TP1 (and TP2 for P2P). Hard — many published bugs (Imine et al. 2003) | Mathematically provable — commutativity is structural |
| **Server compute per op** | O(n) transforms where n = ops since client's revision | Minimal — merge is local to each replica |
| **Client compute per op** | Transform against in-flight + buffer (constant, small) | Insert into CRDT structure — O(log n) for tree-based CRDTs |
| **Memory overhead** | Low — only the live document + recent op log | High — unique IDs per character + tombstones for all deletions |
| **Document size** | Proportional to visible text | Proportional to total edits ever made (tombstones never go away without GC) |
| **Offline support** | Limited — queue ops, transform on reconnect (O(m * n) transforms) | Excellent — merge any number of diverged replicas efficiently |
| **Undo** | Complex — inverse must be transformed against all subsequent ops | Also complex — but some CRDT designs make undo more natural |
| **Intent preservation** | Server can enforce ordering and tiebreaking | Weaker — mathematical convergence may not match user intent. Interleaving anomalies possible (Kleppmann 2019) |
| **Latency model** | Optimistic local apply + server round-trip for confirmation | Optimistic local apply + eventual sync with peers |
| **Rich text** | Extend operation types (retain/insert/delete/format) — O(N^2) transform pairs | Extend CRDT with formatting marks/annotations — varies by implementation |
| **Maturity at scale** | 15+ years at Google (Docs, Slides, Sheets) | Newer — Yjs (2015+), Automerge (2017+), growing rapidly |
| **Garbage collection** | Op log compaction via snapshots (straightforward) | Tombstone GC requires consensus among all replicas (hard for P2P) |
| **Implementation complexity** | Transform function matrix is complex but localized | Data structure is simpler but distributed GC and sync protocols add complexity |

### When to Choose OT vs CRDT

**Choose OT when:**
- You have a reliable, centralized server infrastructure (like Google).
- Memory efficiency matters (no tombstone overhead).
- Server authority is needed (permissions, validation, ordering).
- You want to avoid the tombstone garbage collection problem.
- The system is primarily online-first.

**Choose CRDTs when:**
- Offline-first is a primary requirement.
- Peer-to-peer sync is needed (no central server).
- The data model is naturally decomposable (blocks, objects, key-value pairs).
- You want correctness guarantees from the data structure itself rather than from a complex transform function.

### Why Google Chose OT

1. **Google has reliable, low-latency servers.** The primary disadvantage of OT (requires a central server) is irrelevant — Google runs the servers. CRDT's advantage (no server needed) provides no value in Google's context.

2. **Lower memory overhead.** For a document with 1 million visible characters and 10 million total edits, a CRDT's state could be 10x larger than the visible document due to tombstones and unique character IDs. OT's server state is just the current document + the operation log (which is stored on disk, not in memory).

3. **Server authority.** OT's centralized model naturally supports permission checking, operation validation, and deterministic ordering. The server is the single source of truth. With CRDTs, any replica can generate valid operations — enforcing permissions requires additional mechanisms.

4. **Historical investment.** Google built OT infrastructure for Wave (2009) and evolved it for Docs, Slides, and Sheets. Rewriting to CRDTs would require rebuilding the entire collaboration stack for billions of users.

5. **Simpler correctness model.** With a central server, only TP1 is needed. TP1 is well-understood and testable. P2P CRDTs avoid TP2 by mathematical construction, but introduce their own correctness challenges (interleaving anomalies, tombstone GC safety).

### Why Figma Chose a CRDT-Inspired Approach

Figma's collaborative design tool uses a different model than Google Docs:

- **LWW (Last-Writer-Wins) registers** for object properties (position, size, color, etc.). Each property is independently editable; the last write wins.
- **Fractional indexing** for ordering objects in a layer stack. Instead of integer positions (which cause conflicts), objects have fractional positions (e.g., 0.5 between 0 and 1). New objects can always find a position between any two existing objects.
- **No tombstones** for most operations — design objects are coarse-grained (not individual characters), so the overhead is manageable.

This approach works well for Figma because:
- Design objects are relatively **independent** (moving a rectangle doesn't conflict with changing a circle's color).
- **Object-level granularity** is much coarser than character-level text editing, reducing conflict frequency.
- The server still exists and acts as a relay, but the CRDT-like properties simplify conflict resolution.

It would be harder to apply this approach to text editing, where characters are highly interdependent and operations at the character level conflict frequently.

---

## 9. Complexity Analysis

### Transform Function Matrix: O(N^2)

For N types of operations, the transform function must handle N x N combinations. Each combination requires its own logic.

**Plain text (3 types: retain, insert, delete):**

```
              retain    insert    delete
  retain      r x r     r x i     r x d
  insert      i x r     i x i     i x d
  delete      d x r     d x i     d x d

  = 9 cases (some are symmetric, so ~6 unique)
```

This is manageable. But adding rich text formatting:

**Rich text (5+ types: retain, insert, delete, format, embed):**

```
              retain  insert  delete  format  embed
  retain      r x r   r x i   r x d   r x f   r x e
  insert      i x r   i x i   i x d   i x f   i x e
  delete      d x r   d x i   d x d   d x f   d x e
  format      f x r   f x i   f x d   f x f   f x e
  embed       e x r   e x i   e x d   e x f   e x e

  = 25 cases
```

And this grows further with:
- Table operations (insert row, insert column, merge cells, split cells)
- List operations (indent, outdent, change list type)
- Image operations (resize, crop, position)
- Comment anchor operations

Each new operation type adds an entire row AND column to the matrix. Google Docs' actual OT implementation likely has dozens of operation types, resulting in hundreds of transform pairs.

### Correctness Challenges

The transform function matrix is not only large — **each pair must be provably correct.** Subtle bugs cause documents to diverge silently, which is the worst failure mode (users see different content and don't know it).

**Imine et al. (2003)** formally analyzed several published OT algorithms and found violations:

- **SDT (Sun, Jia, Zhang, Yang, 1998):** Violated TP1 in certain delete-delete scenarios.
- **Several other algorithms:** Failed under specific combinations of operations that hadn't been tested.

The core difficulty: **most bugs only manifest with 3+ concurrent operations in specific configurations.** Simple two-operation tests pass. The bug surfaces when operation C is transformed against the result of transforming A against B, and the interaction produces an unexpected position.

### Testing Strategies

**Property-based testing (QuickCheck / Hypothesis style):**

Generate random document states, random concurrent operations, and verify that:

1. **TP1 holds:** For all pairs of concurrent operations (A, B):
   ```
   apply(apply(doc, A), B') == apply(apply(doc, B), A')
   where (A', B') = transform(A, B)
   ```

2. **Composition is consistent:** For all operations A, B:
   ```
   apply(apply(doc, A), B) == apply(doc, compose(A, B))
   ```

3. **Inversion works:** For all operations A:
   ```
   apply(apply(doc, A), invert(A, doc)) == doc
   ```

4. **Transform + compose interact correctly:**
   ```
   compose(A, B') == compose(B, A')
   where (A', B') = transform(A, B)
   ```

**Fuzzing:**

Generate millions of random operation sequences with 2-5 concurrent clients, simulate the full client-server protocol, and verify that all clients converge to the same document state. This catches bugs that only manifest in multi-step scenarios.

**Formal verification:**

For critical production systems, use formal methods (TLA+, Coq, Isabelle) to prove that the transform function satisfies TP1 for all possible inputs. Google has not publicly disclosed whether they use formal verification for their OT implementation, but given the stakes (billions of documents), it would be prudent.

**Checksumming in production:**

As a safety net, clients periodically compute a checksum (hash) of their local document state and send it to the server. If a client's checksum doesn't match the server's, a **divergence** has been detected. The client forces a reload from the server state. This doesn't prevent bugs — it detects them and limits the damage.

---

## 10. Key Research Papers

### Foundational Papers

| Year | Authors | Title | Key Contribution |
|---|---|---|---|
| **1989** | Ellis & Gibbs | *Concurrency Control in Groupware Systems* (SIGMOD) | **Original OT paper.** Introduced the dOPT algorithm and the concept of operation transformation for collaborative editing. The first formal description of the problem and a solution. |
| **1995** | Nichols, Curtis, Dixon, Lamping | *High-Latency, Low-Bandwidth Windowing in the Jupiter Collaboration System* (UIST) | **Jupiter protocol.** Introduced the client-server OT model with the 2D state space. Key insight: client-server only needs TP1, not TP2. The foundation for Google Docs' OT. |
| **1996** | Ressel, Nitsche-Ruhland, Gunzenhauser | *An Integrating, Transformation-Oriented Approach to Concurrency Control and Undo in Group Editors* (CSCW) | **adOPTed algorithm.** Introduced TP2 for peer-to-peer OT. Proposed a formal framework for OT correctness. |
| **1998** | Sun & Ellis | *Operational Transformation in Real-Time Group Editors: Issues, Algorithms, and Achievements* (CSCW) | **GOT/GOTO algorithms.** Extended OT with history buffers and multi-path transformation. Addressed some of TP2's challenges. |
| **2003** | Imine, Molli, Oster, Rusinowitch | *Proving Correctness of Transformation Functions in Real-Time Groupware* (ECSCW) | **Found bugs in published OT algorithms.** Used formal methods to analyze existing algorithms and demonstrated that several violated their claimed properties. A wake-up call for the OT community. |
| **2009** | David Wang (Google) | *Google Wave Operational Transformation* (Google whitepaper) | **Google's OT protocol.** Documented the three-state client model, the client-server protocol, and the compound operation model used in Google Wave (and later adapted for Google Docs). Publicly available whitepaper. |

### CRDT and Related Papers

| Year | Authors | Title | Key Contribution |
|---|---|---|---|
| **2011** | Shapiro, Preguica, Baquero, Zawirski | *Conflict-free Replicated Data Types* (SSS) | **Formal CRDT framework.** Defined state-based CRDTs (CvRDTs) and operation-based CRDTs (CmRDTs). Established the theoretical foundation for conflict-free replicated data structures. |
| **2011** | Roh, Jeon, Kim, Lee | *Replicated Abstract Data Types: Building Blocks for Collaborative Applications* (JPDC) | **RGA (Replicated Growable Array).** A CRDT for ordered lists / text. Each element has a unique ID. The basis for Automerge's text type. |
| **2016** | Nicolaescu, Jahns, Derntl, Klamma | *Yjs: A Framework for Near Real-Time P2P Shared Editing on Arbitrary Data Types* (ECSCW) | **Yjs and YATA algorithm.** Described the YATA algorithm used in Yjs, with doubly-linked list structure and deterministic insertion ordering. |
| **2019** | Kleppmann, Gomes, Mulligan, Beresford | *Interleaving Anomalies in Collaborative Text Editors* (PaPoC) | **Interleaving anomalies.** Demonstrated that both OT and CRDTs can produce unexpected interleaving of text when users type at the same position concurrently. A fundamental limitation of character-level collaboration. |

### Recommended Reading Order (for interview prep)

1. **David Wang (2009)** — Google Wave OT whitepaper. Start here. Explains the protocol Google uses in practical terms.
2. **Nichols et al. (1995)** — Jupiter protocol. Understand the 2D state space and why client-server simplifies OT.
3. **Shapiro et al. (2011)** — CRDTs. Understand the alternative so you can compare in the interview.
4. **Imine et al. (2003)** — Correctness challenges. Be ready to explain why OT is hard to get right.
5. **Kleppmann (2019)** — Interleaving anomalies. Shows limitations of both OT and CRDTs for text editing.
6. **Ellis & Gibbs (1989)** — The original. Know the history.

---

## Quick Reference: Interview Talking Points

**If asked "How does OT work?":**
> "An operation is a full traversal of the document — a sequence of retain, insert, and delete components. When two users edit concurrently, their operations are based on the same document state but may conflict. The transform function takes two concurrent operations and produces transformed versions that can be applied in either order to reach the same result. This is the TP1 convergence property. Google Docs uses a centralized server that imposes a total order on operations and transforms each incoming operation against all operations that have occurred since the client's base revision."

**If asked "Why not CRDTs?":**
> "CRDTs guarantee convergence without a central server by building commutativity into the data structure. But they have higher memory overhead — each character needs a unique ID, and deletions leave tombstones. Google has reliable centralized servers, so the main advantage of CRDTs (no server needed) doesn't apply. OT gives Google lower memory usage, server authority for permissions, and 15+ years of proven production deployment."

**If asked "What's hard about OT?":**
> "Three things. First, the transform function matrix grows O(N^2) with operation types — adding rich text formatting significantly increases the number of cases. Second, correctness is difficult to prove — Imine et al. (2003) found bugs in multiple published algorithms. Most bugs only surface with 3+ concurrent operations in specific configurations. Third, undo is non-trivial — you can't just reverse your last operation because other users' edits have interleaved. You need to transform the inverse against all subsequent operations."

**If asked "What protocol does Google use?":**
> "A variant of the Jupiter collaboration protocol (Nichols et al., 1995), extended with the three-state client model documented in the Google Wave whitepaper (David Wang, 2009). The three states are Synchronized, Awaiting ACK, and Awaiting ACK with Buffer. The client sends at most one operation at a time and composes additional local edits into a buffer. This limits complexity while keeping the UI responsive through optimistic local application."

---

*End of OT deep dive. Return to [01-interview-simulation.md](01-interview-simulation.md) for the full interview context.*
