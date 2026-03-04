# Deep Dive: Cursor Synchronization & Presence

> **Companion document to [01-interview-simulation.md](01-interview-simulation.md) -- Phase 7**
> This document expands on the cursor synchronization and user presence discussion from the main interview simulation.

---

## Table of Contents

1. [Cursor State Model](#1-cursor-state-model)
2. [Broadcast Mechanism](#2-broadcast-mechanism)
3. [Throttling and Interpolation](#3-throttling-and-interpolation)
4. [Cursor Position Stability Under Edits](#4-cursor-position-stability-under-edits)
5. [Presence (Who's Viewing)](#5-presence-whos-viewing)
6. [User Color Assignment](#6-user-color-assignment)
7. [Scaling: The O(N^2) Problem](#7-scaling-the-on2-problem)
8. [Contrast with Other Systems](#8-contrast-with-other-systems)

---

## 1. Cursor State Model

Every active user in a Google Doc has a cursor state that is tracked and broadcast to all other editors. The cursor state captures everything needed to render another user's position and selection in the document.

### Cursor State Structure

```
CursorState {
  userId:         string     // "alice@gmail.com"
  displayName:    string     // "Alice Chen"
  avatarUrl:      string     // URL to profile picture
  color:          string     // "#4285F4" (Google Blue)
  cursorPosition: number     // character index in the linear document model
  selectionStart: number     // start of selection range (= cursorPosition if no selection)
  selectionEnd:   number     // end of selection range (= cursorPosition if no selection)
  lastActive:     timestamp  // last time this user performed any action
}
```

### What the User Sees

```
Document text with two other editors:

  The quick brown |fox jumps over the lazy dog.
                  ^
                  Alice (blue cursor, with name label "Alice Chen")

  The quick [brown fox jumps] over the lazy dog.
            ^^^^^^^^^^^^^^^^^
            Bob (green selection highlight, with name label "Bob Smith")

Visual elements per remote user:
  1. A colored vertical bar (cursor) at their cursor position
  2. A small name label above the cursor (appears on hover or always, configurable)
  3. A colored highlight over their selection range (if they have text selected)
  4. Avatar in the document header showing they are present
```

### Cursor vs Selection

```
No selection (just a cursor blinking at a position):
  cursorPosition: 42
  selectionStart: 42
  selectionEnd:   42

  Visual: thin colored bar at position 42

Selection range (user has highlighted text):
  cursorPosition: 50  (cursor is at the END of the selection)
  selectionStart: 42
  selectionEnd:   50

  Visual: colored highlight from position 42 to 50,
          with cursor bar at position 50

Backward selection (user selected right-to-left):
  cursorPosition: 42  (cursor is at the START of the selection)
  selectionStart: 42
  selectionEnd:   50

  Visual: same colored highlight, cursor bar at position 42
  (Selection direction matters for extending the selection)
```

---

## 2. Broadcast Mechanism

### Same Channel as Document Operations

Cursor updates are sent over the **same WebSocket connection** used for document operations. This is a deliberate design choice:

```
WebSocket Channel for doc-abc-123:

  Message types on this channel:
    1. Document operations:     insert, delete, format (OT-transformed)
    2. Operation ACKs:          server acknowledges client's operation
    3. Cursor updates:          cursor position changes from other users
    4. Presence events:         user joined, user left
    5. Permission changes:      access level changed
    6. Comment events:          new comment, reply, resolve

  All on ONE WebSocket connection per document per client.
```

**Why share the channel?**

| Alternative | Problem |
|---|---|
| Separate WebSocket for cursors | Two connections per client doubles resource usage. Ordering between cursor updates and document operations becomes difficult -- cursor at position 42 arrives before the insert that created position 42. |
| HTTP polling for cursors | Unacceptable latency (100ms+ per poll round trip). Cursor movement feels laggy and teleport-like instead of smooth. |
| Server-Sent Events (SSE) for cursors | One-directional (server to client only). The client still needs to send cursor updates, requiring a separate channel. |

### Broadcast Flow

```
Alice moves her cursor to position 142:

  Alice's Client                OT Server                Bob's Client
       |                            |                         |
       | cursorUpdate(pos=142)      |                         |
       |--------------------------->|                         |
       |                            |                         |
       |                            | Validate:               |
       |                            |   - Is Alice still      |
       |                            |     connected?          |
       |                            |   - Is pos 142 valid?   |
       |                            |                         |
       |                            | Broadcast to all        |
       |                            | OTHER clients:          |
       |                            |                         |
       |                            | cursorUpdate(           |
       |                            |   userId=alice,         |
       |                            |   pos=142,              |
       |                            |   color="#4285F4")      |
       |                            |------------------------>|
       |                            |                         |
       |                            | (Also to Carol,         |
       |                            |  Dave, Eve, ...)        |
       |                            |                         |

  Note: The server does NOT echo Alice's cursor update back to Alice.
  Alice already knows where her own cursor is.
```

### Message Format (Compact)

```
Cursor update message (sent over WebSocket):

{
  "type": "cursor",
  "u": "alice",           // userId (abbreviated for bandwidth)
  "p": 142,               // cursor position
  "ss": 142,              // selection start
  "se": 142               // selection end
}

Size: ~50-80 bytes per cursor update

For comparison, a typical document operation:
{
  "type": "op",
  "rev": 1234,
  "op": [{"r": 141}, {"i": "X"}, {"r": 500}]
}

Size: ~80-150 bytes per operation
```

---

## 3. Throttling and Interpolation

### The Problem: Cursor Moves A LOT

Users move their cursor constantly -- far more frequently than they edit text:

```
Cursor movement sources and their frequency:

  Action                    Cursor changes/sec   Notes
  -------------------------------------------------------
  Typing                    5-15                 Cursor advances with each character
  Arrow keys (held down)    10-30                Key repeat rate
  Mouse click               1-3                  Repositioning
  Mouse drag (selecting)    30-60                Continuous as mouse moves
  Touch scrolling           0                    Scroll doesn't move cursor
  Ctrl+A (select all)       1                    Single event

  Worst case: Mouse drag selecting text = ~60 cursor changes/second
  Average active editor: ~5-10 cursor changes/second
```

Broadcasting every cursor change would flood the WebSocket:

```
100 editors, each generating 10 cursor changes/sec:
  = 1,000 cursor events/sec arriving at the OT server
  Each broadcast to 99 other clients:
  = 99,000 outbound cursor messages/sec from the OT server

  At 80 bytes per message:
  = 7.9 MB/sec of cursor data alone
  = ~63 Mbps of outbound bandwidth JUST for cursors

  This is unsustainable.
```

### Client-Side Throttling

The solution is to **throttle cursor updates on the client** before sending:

```
Client-side cursor throttle implementation (conceptual):

  THROTTLE_INTERVAL = 50ms  (= 20 updates/sec max)

  lastSentTime = 0
  pendingCursorUpdate = null

  onCursorChange(newPosition, newSelection):
    pendingCursorUpdate = {pos: newPosition, sel: newSelection}

    now = currentTime()
    if (now - lastSentTime >= THROTTLE_INTERVAL):
      sendCursorUpdate(pendingCursorUpdate)
      lastSentTime = now
      pendingCursorUpdate = null
    else:
      // Don't send yet. The next throttle tick will send
      // the LATEST position (not intermediate ones).
      scheduleAfter(THROTTLE_INTERVAL - (now - lastSentTime)):
        if (pendingCursorUpdate != null):
          sendCursorUpdate(pendingCursorUpdate)
          lastSentTime = currentTime()
          pendingCursorUpdate = null

Key insight: We send the LATEST cursor position, not every intermediate one.
If the user moves from pos 100 to pos 200 in 50ms, we send ONE update
for pos 200, not 100 individual updates.
```

### Client-Side Interpolation

On the receiving end, other clients **interpolate** between received cursor positions for smooth visual movement:

```
Rendering another user's cursor:

  Received updates (from Alice):
    t=0ms:     pos=100
    t=50ms:    pos=105
    t=100ms:   pos=112
    t=150ms:   pos=120

  WITHOUT interpolation:
    Cursor teleports every 50ms:
    100 -------- jump -------- 105 -------- jump -------- 112
    Looks jittery and unnatural.

  WITH interpolation:
    Between received positions, smoothly animate the cursor:
    100 → 101 → 102 → 103 → 104 → 105 → 106 → 107 → ...
    CSS transition or requestAnimationFrame-based animation.
    Cursor glides smoothly. Feels natural.

  Implementation:
    onReceiveCursorUpdate(userId, newPos):
      targetPos[userId] = newPos
      // Animation loop will smoothly move toward targetPos

    animationLoop():  // runs at 60fps
      for each remoteUser:
        currentPos = displayedPos[remoteUser]
        target = targetPos[remoteUser]
        if (currentPos != target):
          // Move a fraction of the distance each frame
          // (ease-out interpolation)
          displayedPos[remoteUser] = lerp(currentPos, target, 0.3)
          renderCursor(remoteUser, displayedPos[remoteUser])
```

---

## 4. Cursor Position Stability Under Edits

### The Problem

When another user edits the document, **all cursor positions may need to shift** to remain at the same logical location in the text:

```
Before Bob's edit:
  Document: "The quick brown fox jumps over the lazy dog."
  Alice's cursor at position 20 (between "fox" and " jumps")
                        ^
                       pos 20

Bob inserts "very " at position 10 (between "quick" and "brown"):
  Document: "The quick very brown fox jumps over the lazy dog."

Alice's cursor should now be at position 25 (still between "fox" and " jumps"):
                              ^
                             pos 25

If Alice's cursor stayed at position 20, it would now be between
"very" and " brown" -- the wrong place!
```

### OT Transform for Cursor Positions

Cursor positions are transformed using the **same OT transform logic** used for document operations. The cursor position is treated as a zero-length "retain" at that position:

```
Transform rules for cursor position P against an operation:

  1. INSERT at position I, length L:
     if I <= P:
       P' = P + L     // insert before cursor: shift right
     else:
       P' = P         // insert after cursor: no change

  2. DELETE at position D, length L:
     if D + L <= P:
       P' = P - L     // delete entirely before cursor: shift left
     elif D <= P:
       P' = D         // delete overlaps cursor: cursor moves to deletion point
     else:
       P' = P         // delete entirely after cursor: no change

  3. FORMAT at position F, length L:
     P' = P           // formatting never shifts positions
```

### Worked Examples

```
EXAMPLE 1: Insert before cursor
  Alice cursor at pos 100.
  Bob inserts 5 chars at pos 50.
  Alice cursor: 100 + 5 = 105. (Content before cursor grew.)

EXAMPLE 2: Insert after cursor
  Alice cursor at pos 100.
  Bob inserts 5 chars at pos 150.
  Alice cursor: 100. (No change -- insert is after cursor.)

EXAMPLE 3: Delete before cursor
  Alice cursor at pos 100.
  Bob deletes 3 chars at pos 40 (positions 40, 41, 42).
  Alice cursor: 100 - 3 = 97. (Content before cursor shrank.)

EXAMPLE 4: Delete overlapping cursor
  Alice cursor at pos 100.
  Bob deletes 10 chars starting at pos 95 (positions 95-104).
  Alice cursor: 95. (Cursor was inside the deleted range.
                      It collapses to the deletion point.)

EXAMPLE 5: Selection range adjustment
  Alice has selection [100, 120].
  Bob inserts 5 chars at pos 50.
  Alice selection: [105, 125]. (Both endpoints shift right.)

EXAMPLE 6: Selection partially deleted
  Alice has selection [100, 120].
  Bob deletes chars 110-130.
  Alice selection: [100, 110]. (End of selection truncated
                                to the start of deletion.)
```

### When the Server Broadcasts

The server does not send separate "adjust your cursor" messages. Instead, when the server broadcasts a document operation (e.g., Bob's insert) to Alice, Alice's client:

1. Receives the operation
2. Applies it to the local document model
3. Transforms ALL remote cursor positions against the operation
4. Transforms Alice's OWN cursor position against the operation
5. Re-renders the document and all cursors

This happens automatically as part of applying any incoming operation. No separate cursor adjustment protocol is needed.

---

## 5. Presence (Who's Viewing)

### Presence Indicators

The document header shows **avatars** of all users currently viewing or editing the document:

```
+------------------------------------------------------------------+
|  My Document Title                            [A] [B] [C] [+2]   |
|                                                ^^   ^^   ^^       |
|                                          Alice Bob Carol          |
|                                          (blue)(grn)(red)         |
+------------------------------------------------------------------+
|                                                                   |
|  Document content here...                                         |
|                                                                   |

[A] = Alice's avatar (circular, with blue border matching cursor color)
[B] = Bob's avatar (green border)
[C] = Carol's avatar (red border)
[+2] = 2 more users (collapsed to save space)
```

### Presence State Machine

```
                    WebSocket connected
                    + auth successful
  [NOT PRESENT] ─────────────────────────> [ACTIVE]
       ^                                      │
       │                                      │ No activity
       │                                      │ for 5 minutes
       │                                      v
       │                                 [IDLE]
       │                                      │
       │                                      │ User performs
       │                                      │ any action
       │                                      │ (type, scroll,
       │                                      │  click, etc.)
       │    WebSocket closed                  │
       │    OR heartbeat timeout              v
       ├──────────────────────────────── [ACTIVE]
       │
       │    WebSocket closed
       │    OR heartbeat timeout
       ├──────────────────────────────── [IDLE]
       │
       │

  States:
    NOT PRESENT: User is not connected to this document.
                 No avatar shown. No cursor rendered.

    ACTIVE:      User is connected and recently interacted.
                 Avatar shown with full opacity.
                 Cursor shown (if they have one).

    IDLE:        User is connected but has not interacted recently.
                 Avatar shown with reduced opacity (grayed out).
                 Cursor may be hidden or shown dimly.
                 "Idle" status shown on hover.
```

### Heartbeat Mechanism

```
Presence heartbeat protocol:

  Client → Server: WebSocket ping every 30 seconds
  Server → Client: WebSocket pong

  If server receives no ping from a client for 30 seconds:
    → Client is considered disconnected
    → Server broadcasts "user left" to all other clients
    → Client's cursor is removed from all screens
    → Client's avatar is removed from the header

  If client loses network:
    → Client stops sending pings
    → After 30 seconds, server declares client disconnected
    → If client reconnects within 30 seconds, presence is maintained
       seamlessly (no visible "left and rejoined" event)

  Heartbeat timeline:
    t=0s:   Client sends ping. Server responds pong.
    t=30s:  Client sends ping. Server responds pong.
    t=60s:  [Client loses network]
    t=90s:  Server: no ping received for 30s → disconnect.
            Server broadcasts: "alice left the document."
            Other clients remove Alice's cursor and avatar.
    t=95s:  [Client regains network]
            Client reconnects WebSocket.
            Server broadcasts: "alice joined the document."
            Alice re-enters ACTIVE state.
```

### Multiple Tabs / Devices

```
Scenario: Alice has the same document open in two browser tabs.

  Tab 1: WebSocket connection #1 → OT Server
  Tab 2: WebSocket connection #2 → OT Server

  Server sees TWO connections from Alice.
  Presence: Alice's avatar is shown ONCE (not duplicated).
  Cursors: Each tab has its own cursor position.
           The server tracks both, but only the MOST RECENTLY
           ACTIVE cursor is broadcast to other users.

  If Alice types in Tab 1:
    Tab 1's cursor is broadcast to Bob, Carol, etc.
    Tab 2's cursor is not broadcast (it is stale).

  If Alice closes Tab 1:
    Tab 2's cursor becomes the active one.
    Alice remains present (Tab 2 is still connected).
    Alice's avatar stays in the header.

  If Alice closes BOTH tabs:
    After heartbeat timeout: Alice is marked as disconnected.
    Avatar removed. Cursor removed.
```

---

## 6. User Color Assignment

### The Color Palette

Google Docs assigns each editor a distinct color from a fixed palette of approximately 20 colors:

```
Google Docs Editor Color Palette (approximate):

  Color Name          Hex Code     Swatch
  ─────────────────────────────────────────
  1.  Google Blue     #4285F4      ████
  2.  Google Red      #EA4335      ████
  3.  Google Yellow   #FBBC04      ████
  4.  Google Green    #34A853      ████
  5.  Purple          #A142F4      ████
  6.  Teal            #24C1E0      ████
  7.  Orange          #FA7B17      ████
  8.  Pink            #F538A0      ████
  9.  Deep Purple     #6200EA      ████
  10. Cyan            #00ACC1      ████
  11. Amber           #FFB300      ████
  12. Lime            #9E9D24      ████
  13. Deep Orange     #E64A19      ████
  14. Indigo          #3949AB      ████
  15. Light Green     #7CB342      ████
  16. Brown           #6D4C41      ████
  17. Blue Grey       #546E7A      ████
  18. Dark Cyan       #00838F      ████
  19. Deep Teal       #00695C      ████
  20. Magenta         #AD1457      ████

  Design constraints:
    - All colors must be readable against a white background
    - All colors must be distinguishable from each other
    - Colors must work for cursor bars, selection highlights,
      name labels, and avatar borders
    - Selection highlights use the color at ~20% opacity
      (so they don't obscure the text)
```

### Assignment Strategy

```
Color assignment algorithm:

  When a user joins a document editing session:
    1. Server checks the list of currently active editors.
    2. Server finds the first color in the palette NOT
       currently assigned to any active editor.
    3. Assigns that color to the new user.
    4. Broadcasts the assignment to all clients.

  colors_in_use = set()

  onUserJoin(user):
    for color in COLOR_PALETTE:       // iterate in fixed order
      if color not in colors_in_use:
        assign(user, color)
        colors_in_use.add(color)
        broadcast(user, color)
        return
    // All 20 colors in use (20+ editors):
    // Reuse a color (two editors will share a color).
    // This is rare -- most documents have < 20 editors.
    assign(user, COLOR_PALETTE[hash(user.id) % 20])

  onUserLeave(user):
    colors_in_use.remove(user.color)
    // Color becomes available for the next joiner.

  Consequence: A user's color may change between sessions.
    Session 1: Alice gets blue (first to join).
    Session 2: Bob joins first (gets blue), Alice joins second (gets red).
    This is acceptable -- colors are session-level, not identity-level.
```

### Color Used For

| Visual Element | How Color is Applied |
|---|---|
| **Cursor bar** | Solid color, 2px wide vertical line |
| **Name label** | Colored background with white text, positioned above cursor |
| **Selection highlight** | Color at 20% opacity, overlaid on selected text |
| **Avatar border** | Colored ring around the user's profile picture in the header |
| **Comment highlight** | When hovering a comment, the anchor range is highlighted in the commenter's color |

---

## 7. Scaling: The O(N^2) Problem

### The Fundamental Issue

Cursor and presence updates create an **O(N^2) message amplification** problem:

```
With N simultaneous editors:

  Each editor sends:    ~10 cursor updates/sec (after throttling)
  Total inbound:        N * 10 cursor updates/sec
  Each update is broadcast to N-1 other editors.
  Total outbound:       N * 10 * (N-1) messages/sec

  N=10:    10 * 10 * 9   =     900 messages/sec    (manageable)
  N=25:    25 * 10 * 24  =   6,000 messages/sec    (noticeable)
  N=50:    50 * 10 * 49  =  24,500 messages/sec    (heavy)
  N=100:  100 * 10 * 99  =  99,000 messages/sec    (limit)

  At 80 bytes per cursor message:
  N=100:  99,000 * 80 = 7.9 MB/sec = ~63 Mbps outbound

  This is for ONE document.
  The OT server handling this document must sustain this
  bandwidth in addition to document operations.
```

### Bandwidth Breakdown at N=100

```
Per-document bandwidth at 100 editors:

  Component                Messages/sec   Bytes/sec    Mbps
  ──────────────────────────────────────────────────────────
  Cursor broadcasts         99,000        7.9 MB/s     63.2
  Document operations        ~500         50 KB/s       0.4
  Operation ACKs             ~500         25 KB/s       0.2
  Presence events              ~1           100 B/s     0.0
  ──────────────────────────────────────────────────────────
  TOTAL                    ~100,000       ~8.0 MB/s    ~64.0

  Cursor broadcasts dominate: 99.3% of messages, 98.8% of bandwidth.

  This is why the 100-editor cap exists.
```

### Mitigation Strategies

#### Strategy 1: Viewport-Based Culling

Only show and transmit cursors for users editing text **visible in the current viewport**:

```
Document with 100 editors:

  +────────────────────────────+
  |  Page 1-2 (viewport)       |  ← Alice is viewing this
  |  Editors here: Bob, Carol  |  ← Only 2 cursors shown
  +────────────────────────────+
  |  Page 3-5                   |
  |  Editors here: Dave, Eve,  |  ← Alice doesn't see these cursors
  |  Frank, Grace, ...         |
  +────────────────────────────+
  |  Page 6-10                  |
  |  Editors here: 90+ others  |  ← Definitely not shown
  +────────────────────────────+

Implementation:
  Client tells server its viewport range:
    viewportUpdate(startPos=0, endPos=2000)

  Server only sends cursor updates from users whose cursor
  is within or near Alice's viewport.

  Result: Alice receives cursor updates from 2-5 users
          instead of 99.
```

#### Strategy 2: Reduced Frequency for Distant Cursors

```
Cursor update frequency based on distance from viewer:

  Distance from viewer's viewport   Update frequency
  ──────────────────────────────────────────────────
  WITHIN viewport                   10-20 updates/sec (full rate)
  NEAR viewport (within 1 page)     5 updates/sec
  FAR from viewport (2+ pages)      1 update/sec
  VERY FAR (10+ pages)              0.2 updates/sec (every 5 sec)

  Result: Total cursor messages to Alice drops dramatically:
    2 in-viewport users * 15/sec    =  30
    3 near-viewport users * 5/sec   =  15
    10 far users * 1/sec            =  10
    85 very-far users * 0.2/sec     =  17
    ─────────────────────────────────────
    Total:                             72 messages/sec

    vs 990 messages/sec without optimization (93% reduction)
```

#### Strategy 3: The 100-Editor Cap

```
Google Docs enforces a maximum of 100 simultaneous EDITORS.
Additional users can JOIN as VIEWERS (read-only, no cursor broadcast).

  Why 100?
    At N=100 with mitigations, the OT server can sustain:
    - ~1,000 cursor messages/sec (after viewport culling)
    - ~500 document operations/sec
    - ~100 WebSocket connections

    This is well within a single server's capacity.

    At N=200:
    - Even with mitigations, the per-document load doubles.
    - OT transform cost increases linearly with editors.
    - The risk of a "hot document" overwhelming its OT server grows.

    At N=1000:
    - Not feasible for real-time editing with cursors.
    - This is presentation mode: 1 presenter, 999 viewers.
    - Viewers don't need cursor broadcast (they just watch).
```

#### Strategy 4: Server-Side Aggregation (Batching)

```
Instead of sending individual cursor messages:

  Without batching:
    t=0ms:   send cursorUpdate(alice, pos=100)
    t=3ms:   send cursorUpdate(bob, pos=200)
    t=7ms:   send cursorUpdate(carol, pos=300)
    t=12ms:  send cursorUpdate(alice, pos=102)
    ...
    = many small messages, each with WebSocket framing overhead

  With batching (every 50ms):
    t=0-50ms:  accumulate all cursor changes
    t=50ms:    send ONE message with ALL cursor updates:
               {
                 "type": "cursor_batch",
                 "cursors": [
                   {"u": "alice", "p": 102},
                   {"u": "bob",   "p": 200},
                   {"u": "carol", "p": 305}
                 ]
               }

  Benefits:
    - Fewer WebSocket frames (less framing overhead)
    - Fewer network interrupts on the client
    - Easier to process in bulk on the client
    - Only the LATEST position per user is sent (not intermediate)

  Cost:
    - Up to 50ms additional latency on cursor updates
    - Acceptable: cursor movement does not need sub-10ms precision
```

### Combined Effect of Mitigations

```
Cursor messages per second received by one client (100 editors):

  No mitigations:                    99 * 10 = 990 msg/sec

  After throttling (10/sec):                   990 msg/sec (same -- throttling
                                                is already assumed)

  After viewport culling:                      ~72 msg/sec

  After server-side batching:                  ~20 batches/sec
                                                (each containing 3-4 updates)

  Total bandwidth per client:                  ~3 KB/sec (negligible)

  Conclusion: With all mitigations, cursor synchronization is
  NOT a bandwidth bottleneck, even at 100 editors.
```

---

## 8. Contrast with Other Systems

### Google Docs vs Figma

```
+----------------------------+----------------------------+
|        GOOGLE DOCS         |          FIGMA             |
+----------------------------+----------------------------+
| Cursor: 1D position       | Cursor: 2D canvas position |
| (character index in        | (x, y coordinates on the  |
| linear document)           | design canvas)             |
+----------------------------+----------------------------+
| Cursor stability: must    | Cursor stability: simpler  |
| transform position when   | -- 2D coordinates are not  |
| text is inserted/deleted   | affected by other users'   |
| (OT transform on cursor)  | design element edits       |
|                            | (unless elements are moved |
|                            |  under the cursor)         |
+----------------------------+----------------------------+
| Shows: cursor bar +       | Shows: cursor arrow +      |
| selection highlight +      | name label + viewport      |
| name label                | rectangle (what the user   |
|                            | is looking at on the       |
|                            | canvas)                    |
+----------------------------+----------------------------+
| Viewport: scrollable      | Viewport: pannable/zoomable|
| vertical document         | 2D infinite canvas         |
| (1D viewport range)       | (2D viewport rectangle)    |
+----------------------------+----------------------------+
| Scaling: O(N^2) for N     | Scaling: O(N^2) similar    |
| editors, mitigated by     | problem. Figma mitigates   |
| viewport culling in 1D    | by only showing cursors    |
|                            | within the viewer's 2D     |
|                            | viewport rectangle.        |
+----------------------------+----------------------------+
| Color: ~20-color palette   | Color: Similar palette,    |
| assigned by join order    | plus user initials shown   |
|                            | next to cursor for quick   |
|                            | identification             |
+----------------------------+----------------------------+

Figma-specific: Viewport Rectangles
  Figma shows each user's viewport as a colored rectangle on the canvas.
  You can see exactly what area of the design each team member is
  looking at. Google Docs does NOT have this -- you only see cursors,
  not what part of the document others are scrolled to.

  Why Google Docs doesn't need it:
    Documents are 1D (vertical scroll). You can infer roughly where
    someone is by their cursor position relative to yours.
    In a 2D canvas, there is no implicit ordering -- viewport
    rectangles provide essential spatial context.
```

### Google Docs vs VS Code Live Share

```
+----------------------------+----------------------------+
|        GOOGLE DOCS         |    VS CODE LIVE SHARE      |
+----------------------------+----------------------------+
| "Follow" mode: not        | "Follow" mode: one user    |
| a primary feature          | can follow another's       |
|                            | viewport -- auto-scrolls   |
|                            | to wherever the host is    |
|                            | editing                    |
+----------------------------+----------------------------+
| Cursor colors: automatic  | Cursor colors: automatic   |
| from palette              | from palette (similar)     |
+----------------------------+----------------------------+
| Multiple files: N/A       | Multiple files: shows      |
| (single document)          | which FILE each user is    |
|                            | editing, not just cursor   |
|                            | position within a file     |
+----------------------------+----------------------------+
| Presence: avatar in        | Presence: user list in     |
| document header            | sidebar with file/line     |
|                            | info                       |
+----------------------------+----------------------------+
```

---

## Interview Tips: What to Emphasize

### L6 Expectations for Cursor & Presence

When discussing cursor synchronization in a system design interview, an L6 candidate should:

1. **Identify the O(N^2) problem unprompted.** Before the interviewer asks about scaling, raise the fact that N editors broadcasting to N-1 others creates quadratic message amplification. Then propose mitigations.

2. **Explain cursor position stability under OT.** Show that cursor positions are transformed using the same OT logic as document operations. Give a concrete example (insert before cursor shifts cursor right).

3. **Discuss throttling and interpolation together.** Throttling alone makes cursors jittery. Interpolation on the receiving end makes throttled updates look smooth. The two work together.

4. **Know why the 100-editor limit exists.** It is not arbitrary -- it bounds the O(N^2) cursor broadcast and per-document OT server load. Additional users can join as viewers.

5. **Explain presence as ephemeral state.** Presence is not persisted -- it is maintained via WebSocket heartbeats with a timeout. No database writes for "user is viewing."

### L7 and Beyond

An L7 candidate would additionally discuss:
- Multi-device presence handling (same user, two tabs -- show one avatar, track two cursors)
- Priority queuing for cursor updates (nearby cursors get higher priority in the batch)
- Cursor interpolation algorithms (linear vs ease-out vs spring-based)
- Bandwidth estimation for cursor updates and how it compares to document operation bandwidth
- Cursor rendering performance in the browser (100 cursor DOM elements with animations)

---

*This is a companion document to the main interview simulation. For the full interview dialogue, see [01-interview-simulation.md](01-interview-simulation.md).*
*For document storage, see [04-document-storage.md](04-document-storage.md).*
*For conflict resolution, see [06-conflict-resolution-and-consistency.md](06-conflict-resolution-and-consistency.md).*
