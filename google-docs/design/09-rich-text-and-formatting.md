# 09 - Rich Text and Formatting

## Overview

Rich text is what makes Google Docs a **document editor** rather than a plain text editor. Supporting bold, italic, headings, tables, images, and links — while maintaining real-time collaboration via OT — is one of the hardest engineering challenges in the system. The document model, the internal representation, and the OT operation types are all deeply intertwined.

---

## 1. Document Model as a Tree

Google Docs internally represents a document as a **tree of elements**, not as a flat string or raw HTML.

```
Document (root)
├── Header
│   ├── Title: "Q4 Strategy Report"
│   └── Subtitle: "Confidential"
│
├── Section
│   ├── Heading (level=1): "Executive Summary"
│   │
│   ├── Paragraph
│   │   ├── TextRun {text: "Revenue grew ", bold: false}
│   │   ├── TextRun {text: "23%", bold: true, color: green}
│   │   └── TextRun {text: " year-over-year.", bold: false}
│   │
│   ├── Paragraph
│   │   ├── TextRun {text: "Key drivers: "}
│   │   └── Link {text: "cloud adoption", url: "https://..."}
│   │
│   └── List (ordered)
│       ├── ListItem: "Enterprise contracts (+40%)"
│       ├── ListItem: "Consumer subscriptions (+15%)"
│       └── ListItem: "Advertising revenue (+8%)"
│
├── Section
│   ├── Heading (level=1): "Financial Details"
│   │
│   ├── Table (3 rows × 4 cols)
│   │   ├── Row [Header]: ["Metric", "Q3", "Q4", "Change"]
│   │   ├── Row: ["Revenue", "$2.1B", "$2.6B", "+23%"]
│   │   └── Row: ["Profit", "$420M", "$580M", "+38%"]
│   │
│   ├── Paragraph
│   │   ├── TextRun {text: "See "}
│   │   ├── InlineImage {src: "chart_url", width: 400}
│   │   └── TextRun {text: " for trend analysis."}
│   │
│   └── PageBreak
│
└── Footer
    └── TextRun {text: "Page ", auto_page_number: true}
```

### 1.1 Element Categories

#### Block Elements (own line, stack vertically)

| Element      | Description                       | OT Complexity |
|--------------|-----------------------------------|:-------------:|
| Paragraph    | Basic text container              | Low           |
| Heading      | H1-H6 with semantic level        | Low           |
| List         | Ordered/unordered, nestable       | Medium        |
| Table        | Rows and columns, mergeable cells | Very High     |
| Image        | Block-level image with caption    | Medium        |
| PageBreak    | Force new page                    | Low           |
| HorizontalRule | Divider line                    | Low           |
| Code Block   | Monospaced, syntax-highlighted    | Medium        |

#### Inline Elements (flow within a paragraph)

| Element       | Description                       | OT Complexity |
|---------------|-----------------------------------|:-------------:|
| TextRun       | Contiguous text with same formatting | Low        |
| InlineImage   | Image within text flow            | Medium        |
| Link          | Hyperlink wrapping text           | Medium        |
| Mention       | @-mention of a user               | Low           |
| Bookmark      | Named anchor for cross-references | Low           |
| FootnoteRef   | Reference to a footnote           | Medium        |
| Equation      | LaTeX-style inline math           | Medium        |

### 1.2 TextRun: The Atomic Unit

A **TextRun** is a contiguous span of text that shares identical formatting attributes. When formatting changes, TextRuns split or merge.

```
Before bolding "grew 23%":
  Paragraph
  └── TextRun {text: "Revenue grew 23% year-over-year.", bold: false}

After bolding "grew 23%":
  Paragraph
  ├── TextRun {text: "Revenue ", bold: false}
  ├── TextRun {text: "grew 23%", bold: true}
  └── TextRun {text: " year-over-year.", bold: false}

After also italicizing "23%":
  Paragraph
  ├── TextRun {text: "Revenue ", bold: false, italic: false}
  ├── TextRun {text: "grew ", bold: true, italic: false}
  ├── TextRun {text: "23%", bold: true, italic: true}
  └── TextRun {text: " year-over-year.", bold: false, italic: false}
```

Adjacent TextRuns with identical formatting are **merged** to avoid fragmentation:
```
If the user un-bolds "grew ":
  Before: [TextRun("Revenue ", {}), TextRun("grew ", {b}), TextRun("23%", {b,i}), ...]
  After:  [TextRun("Revenue grew ", {}), TextRun("23%", {b,i}), ...]
  ─── "Revenue " and "grew " merged because they now share the same formatting
```

---

## 2. Formatting as OT Operations

### 2.1 The Format Operation

In addition to `insert` and `delete`, the OT system supports a `format` operation:

```
Operation types:
  insert(position, text, attributes)
  delete(position, length)
  format(startIndex, endIndex, attributeChanges)
```

```
Example format operations:

  // Bold characters 5 through 10
  format(5, 10, {bold: true})

  // Remove italic from characters 0 through 20
  format(0, 20, {italic: null})     // null means "remove this attribute"

  // Set font size for characters 12 through 18
  format(12, 18, {fontSize: 14})

  // Apply multiple attributes at once
  format(5, 10, {bold: true, color: "#FF0000", underline: true})
```

### 2.2 Transforming Format Operations

Format operations must be **transformed** against insert and delete operations, just like inserts transform against deletes. This is where the complexity explodes.

#### Example: Insert vs. Format

```
Initial document: "Hello World" (11 chars, indices 0-10)

Alice: format(6, 11, {bold: true})      → Bold "World"
Bob:   insert(8, "Big ")                 → Insert "Big " at position 8

Without transformation:
  Alice's format bolds indices 6-11 → "World" is bold ✓
  Bob's insert adds "Big " at 8    → "HeBig llo World"  ✗ Wrong position!

With OT transformation:
  Bob's insert is at position 8, which is inside Alice's format range [6, 11]

  Transform Alice's format against Bob's insert:
    Bob inserted 4 chars at pos 8
    Alice's range [6, 11] → [6, 15]  (endIndex shifts by 4 because insert is within range)

  Result: "Hello Big World"
    format(6, 15, {bold: true})  → "Big World" is bold ✓
    "Big " inherits the bold because it was inserted within the bold range ✓
```

#### Example: Delete vs. Format

```
Initial document: "Hello Beautiful World" (21 chars)

Alice: format(6, 21, {bold: true})     → Bold "Beautiful World"
Bob:   delete(6, 10)                    → Delete "Beautiful " (10 chars)

Transform Alice's format against Bob's delete:
  Bob deleted 10 chars starting at pos 6
  Alice's range [6, 21]:
    Start (6) is at the deletion point → stays at 6
    End (21) shifts left by 10       → becomes 11

  Result: "Hello World"
    format(6, 11, {bold: true})  → "World" is bold ✓
```

#### Example: Format vs. Format (Same Range, Different Attributes)

```
Initial: "Hello World"

Alice: format(0, 11, {bold: true})
Bob:   format(0, 11, {italic: true})

These are independent attributes — both apply:
  Result: "Hello World" is bold AND italic ✓
  No conflict.
```

#### Example: Format vs. Format (Same Range, Same Attribute, Different Values)

```
Initial: "Hello World"

Alice: format(0, 11, {fontSize: 14})
Bob:   format(0, 11, {fontSize: 18})

Conflict! Same attribute, different values.
Resolution: last-writer-wins based on server ordering.
  If Alice's op arrives first → fontSize=14, then Bob's op → fontSize=18
  Final: fontSize = 18
```

### 2.3 Full Transformation Matrix

Every pair of operation types needs a transformation function:

```
              insert      delete      format
           ┌───────────┬───────────┬───────────┐
  insert   │ insert×   │ insert×   │ insert×   │
           │ insert    │ delete    │ format    │
           ├───────────┼───────────┼───────────┤
  delete   │ delete×   │ delete×   │ delete×   │
           │ insert    │ delete    │ format    │
           ├───────────┼───────────┼───────────┤
  format   │ format×   │ format×   │ format×   │
           │ insert    │ delete    │ format    │
           └───────────┴───────────┴───────────┘

  = 9 transformation function pairs (3 × 3)
```

For plain text OT (insert + delete only), there are **4 transformation pairs**. Adding `format` increases this to **9** — more than double. And we haven't even considered table operations yet.

---

## 3. Table Operations: The Complexity Monster

Tables introduce **structural** operations that interact with **content** operations in complex ways.

### 3.1 Table-Specific Operations

```
Table operations:
  insertRow(tableId, rowIndex)
  deleteRow(tableId, rowIndex)
  insertColumn(tableId, colIndex)
  deleteColumn(tableId, colIndex)
  mergeCells(tableId, startRow, startCol, endRow, endCol)
  splitCell(tableId, row, col, numRows, numCols)
  resizeColumn(tableId, colIndex, width)
```

### 3.2 Table × Text Transformation

```
Scenario: Alice inserts a row while Bob types in a cell

Alice: insertRow(table1, rowIndex=2)     → Insert row at index 2
Bob:   insert(pos=145, "quarterly")       → Type in cell at row 3, col 1

The text position (145) refers to a global character index.
Inserting a row shifts all content in rows >= 2 by the character length
of the new (empty) row's structural markers.

Transform: Bob's position must shift by the structural overhead of the new row.
  If the new row adds 12 characters of structural content:
    Bob's insert becomes insert(pos=157, "quarterly")

This is why tables are "Very High" OT complexity.
```

### 3.3 Merge Cells Conflict

```
Alice: mergeCells(table1, row=1, col=1, endRow=2, endCol=2)  → Merge 2×2 block
Bob:   insertRow(table1, rowIndex=2)                           → Insert row between

Conflict: The merge spans rows 1-2, but Bob inserts a new row at index 2.
Resolution options:
  1. Reject Bob's insert (merge takes priority)
  2. Expand merge to include new row (merge becomes rows 1-3)
  3. Apply insert first, then merge only rows 1-2 (row 3 is unmerged)

Google Docs uses option 3: server ordering determines which op applies first,
and the second op is transformed to accommodate the first.
```

---

## 4. Internal Representation

### 4.1 Why NOT HTML?

```html
<!-- HTML representation of "Hello World" with bold "World" -->
<p>Hello <strong>World</strong></p>
```

Problems with HTML for OT:
1. **Multiple valid representations**: `<b><i>text</i></b>` = `<i><b>text</b></i>`. OT requires a canonical form.
2. **DOM tree operations**: Inserting text might require splitting DOM nodes, which creates complex tree-diffing problems.
3. **Tag soup**: Overlapping tags (`<b>Hello <i>World</b> Foo</i>`) are invalid but can arise from concurrent edits.
4. **Attribute overhead**: HTML attributes are string-typed and verbose.
5. **Security**: Raw HTML invites XSS vulnerabilities in a multi-user system.

### 4.2 Why NOT Markdown?

```markdown
Hello **World**
```

Problems with Markdown for OT:
1. **Formatting markers are text**: `**` is two characters that participate in OT. Deleting one `*` breaks the bold.
2. **Context-sensitive parsing**: `*italic*` vs `**bold**` vs `***bold italic***` — the meaning of `*` depends on neighbors.
3. **Limited expressiveness**: No native tables-with-merged-cells, no inline images with positioning, no colored text.
4. **Ambiguous syntax**: Different Markdown parsers produce different results for edge cases.

### 4.3 Google's Custom Intermediate Representation

Google uses a **custom representation** optimized specifically for OT operations. While the exact format is proprietary, a reasonable model based on public information:

```json
{
  "documentId": "doc_abc123",
  "body": {
    "content": [
      {
        "elementType": "PARAGRAPH",
        "paragraphStyle": {
          "headingLevel": 0,
          "alignment": "START",
          "lineSpacing": 1.15,
          "indentStart": 0
        },
        "elements": [
          {
            "elementType": "TEXT_RUN",
            "startIndex": 0,
            "endIndex": 8,
            "content": "Revenue ",
            "textStyle": {
              "bold": false,
              "italic": false,
              "fontSize": 11,
              "fontFamily": "Arial",
              "foregroundColor": "#000000"
            }
          },
          {
            "elementType": "TEXT_RUN",
            "startIndex": 8,
            "endIndex": 16,
            "content": "grew 23%",
            "textStyle": {
              "bold": true,
              "italic": false,
              "fontSize": 11,
              "fontFamily": "Arial",
              "foregroundColor": "#00AA00"
            }
          },
          {
            "elementType": "TEXT_RUN",
            "startIndex": 16,
            "endIndex": 31,
            "content": " year-over-year.",
            "textStyle": {
              "bold": false,
              "italic": false,
              "fontSize": 11,
              "fontFamily": "Arial",
              "foregroundColor": "#000000"
            }
          }
        ]
      }
    ]
  }
}
```

**Key design properties:**

| Property | Why It Matters |
|----------|---------------|
| **Global character indices** | Every character has a unique index across the entire document, making OT positions unambiguous |
| **Flat attributes per TextRun** | No nested formatting — each TextRun carries a complete, flat set of attributes |
| **Canonical ordering** | There is exactly one valid representation for any document state |
| **Structural elements carry indices** | Tables, images, page breaks each consume index positions, so they participate in OT |
| **No raw HTML/CSS** | Rendering is done by Google's own engine, not a browser DOM |

### 4.4 How Operations Map to This Representation

```
User action:         Types "Q" at position 8
Internal operation:  insert(8, "Q", {bold: true, fontSize: 11, ...})

Document update:
  Before: [TextRun(0-8, "Revenue "), TextRun(8-16, "grew 23%"), ...]
  After:  [TextRun(0-8, "Revenue "), TextRun(8-17, "Qgrew 23%"), ...]
  All subsequent indices shift by 1.

User action:         Bolds characters 0-8
Internal operation:  format(0, 8, {bold: true})

Document update:
  Before: [TextRun(0-8, "Revenue ", {bold:false}), TextRun(8-17, ...)]
  After:  [TextRun(0-8, "Revenue ", {bold:true}), TextRun(8-17, ...)]
  Adjacent TextRuns now have same formatting → merge:
  After merge: [TextRun(0-17, "Revenue Qgrew 23%", {bold:true}), ...]
```

---

## 5. Contrast with Other Editors

### 5.1 Markdown-Based Editors (Notion, HackMD)

```
┌─────────────────────────────────────────────────────────┐
│  Markdown Editor (HackMD / Notion)                      │
│                                                         │
│  Document: "Hello **World**"                            │
│                                                         │
│  OT operates on: raw text including ** markers          │
│  Pros:                                                  │
│    - OT is just plain-text insert/delete                │
│    - Much simpler transformation functions              │
│    - No format operation type needed                    │
│    - Fewer edge cases                                   │
│                                                         │
│  Cons:                                                  │
│    - Not WYSIWYG (user sees or must understand markup)  │
│    - Deleting one * breaks formatting                   │
│    - Limited formatting options                         │
│    - Bold marker ** is 2 chars, takes up cursor space   │
│                                                         │
│  Notion's approach: block-based CRDT, not character OT  │
│    - Each block (paragraph, heading, etc.) is a CRDT    │
│    - Rich text within a block uses attribute-based runs │
│    - Simpler than Google Docs because blocks are        │
│      independently edited (no cross-block OT)           │
└─────────────────────────────────────────────────────────┘
```

### 5.2 HTML-Based Editors (CKEditor, TinyMCE)

```
┌─────────────────────────────────────────────────────────┐
│  HTML-Based Editor (CKEditor / TinyMCE)                 │
│                                                         │
│  Document: <p>Hello <b>World</b></p>                    │
│                                                         │
│  OT operates on: DOM tree nodes                         │
│  Pros:                                                  │
│    - WYSIWYG rendering via browser DOM                  │
│    - Rich formatting via CSS                            │
│    - Standards-based                                    │
│                                                         │
│  Cons:                                                  │
│    - DOM tree OT is significantly more complex          │
│    - Multiple valid DOM trees for same visual output    │
│    - Node splitting/merging on format changes           │
│    - contentEditable browser API is notoriously buggy   │
│    - Cross-browser inconsistencies in DOM mutations     │
│    - XSS risk from user-generated HTML                  │
│                                                         │
│  Tree OT (operating on DOM) is an open research problem │
│  with far more edge cases than linear OT.               │
└─────────────────────────────────────────────────────────┘
```

### 5.3 Comparison Summary

| Aspect | Google Docs (Custom) | Markdown Editor | HTML/DOM Editor |
|--------|---------------------|-----------------|-----------------|
| OT complexity | Medium-High (3 op types: insert, delete, format) | Low (2 op types: insert, delete) | Very High (tree operations) |
| WYSIWYG | Full | Partial (preview pane) or rendered blocks | Full |
| Formatting richness | Very High | Low-Medium | High |
| Canonical form | Guaranteed by design | Mostly (some ambiguity) | Not guaranteed (many valid DOMs) |
| Concurrent edit safety | Proven at Google scale | Proven for plain text | Fragile at scale |
| Table support | Full (merge, resize) | Limited (plain text tables) | Full but complex OT |

---

## 6. Rendering Pipeline

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Internal     │     │   Layout Engine   │     │   Canvas / DOM   │
│  Repr (JSON)  │────>│   (Line break,    │────>│   Rendering      │
│               │     │    pagination,    │     │                  │
│               │     │    column flow)   │     │                  │
└──────────────┘     └──────────────────┘     └──────────────────┘

1. Internal Representation
   - Tree of elements with flat attributes
   - This is what OT operates on

2. Layout Engine
   - Computes line breaks based on container width
   - Handles pagination (page margins, headers/footers)
   - Positions tables, images, floated elements
   - Generates a layout tree with pixel positions

3. Rendering
   - Google Docs uses CANVAS rendering (not DOM)
   - The entire document is painted onto an HTML5 <canvas>
   - This avoids contentEditable bugs entirely
   - Custom cursor rendering, custom text selection
   - Custom scrollbar behavior

Why canvas instead of DOM?
   - Full control over rendering (pixel-perfect across browsers)
   - No contentEditable — eliminates a huge class of browser bugs
   - Consistent behavior: Chrome, Firefox, Safari render identically
   - Cost: must reimplement text input, cursor, selection, accessibility
```

---

## 7. Interview Talking Points

**If asked "How do you represent rich text for OT?":**
> We use a custom intermediate representation — not HTML, not Markdown. The document is a tree of elements, where each leaf TextRun carries a flat set of formatting attributes and a global character index range. OT operates on this linear index space with three operation types: insert, delete, and format. The key insight is that formatting is **not** structural (like DOM nodes) — it's attribute ranges on a linear sequence, which keeps OT tractable.

**If asked "Why not just use HTML?":**
> HTML has no canonical form — `<b><i>text</i></b>` and `<i><b>text</b></i>` are visually identical but structurally different. OT requires a single canonical state to converge. Also, DOM tree OT is an open research problem with far more edge cases than linear OT. And contentEditable is notoriously buggy across browsers — Google Docs actually renders on canvas to avoid it entirely.

**If asked "What makes tables so hard?":**
> Tables introduce structural operations — insertRow, deleteRow, mergeCells — that interact with text operations inside cells. A text insert at global position 145 might refer to row 3, cell 2. If someone inserts a row above, that position shifts by the structural overhead of the new row. You need to transform structural operations against content operations and vice versa. The transformation matrix for tables alone has dozens of pairs.

**If asked "How does formatting interact with concurrent edits?":**
> When Alice bolds characters 5-10 and Bob inserts 3 characters at position 7, the OT server transforms Alice's format range to 5-13 — expanding it to include Bob's insertion. The semantics are: if you insert text inside a formatted region, the inserted text inherits that formatting. This matches user expectations and is the standard OT behavior for format-vs-insert transformation.
