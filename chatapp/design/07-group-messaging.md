# Group Chat Architecture — Deep Dive

> **Context:** This document is a deep-dive companion to `01-interview-simulation.md`. It covers group messaging for a WhatsApp-like chat application, contrasting with Discord, Telegram, and Slack where architecturally significant.

---

## Table of Contents

1. [Group Creation and Management](#1-group-creation-and-management)
2. [Fan-Out Strategies](#2-fan-out-strategies)
3. [Group Message Delivery](#3-group-message-delivery)
4. [Group E2E Encryption with Sender Keys](#4-group-e2e-encryption-with-sender-keys)
5. [Ordering in Groups](#5-ordering-in-groups)
6. [Admin Controls](#6-admin-controls)
7. [Group Size Evolution](#7-group-size-evolution)
8. [Contrast with Discord Servers](#8-contrast-with-discord-servers)
9. [Contrast with Telegram Groups/Channels](#9-contrast-with-telegram-groupschannels)
10. [Contrast with Slack Channels](#10-contrast-with-slack-channels)

---

## 1. Group Creation and Management

### Group Lifecycle

```
 Creator                    Server                        Members
   |                          |                              |
   |-- POST /groups --------->|                              |
   |   {name, members[]}      |                              |
   |                          |-- Generate groupId           |
   |                          |-- Creator = admin            |
   |                          |-- Notify each member ------->|
   |<-- 201 {groupId} -------|                              |
   |                          |                              |
   |   (Members receive group invitation via WebSocket)      |
```

### Creator Becomes Admin

When a user creates a group, the server automatically assigns them the **admin** role. This is the only role promotion that happens without an existing admin's action. The creator can then:

- Add or remove members
- Promote other members to admin or demote admins to regular members
- Change the group name, description, and photo
- Modify group settings

### Group Settings

| Setting | Options | Default | Description |
|---------|---------|---------|-------------|
| **Who can send messages** | All members / Only admins | All members | "Only admins" turns the group into a broadcast channel |
| **Who can edit group info** | All members / Only admins | All members | Controls name, photo, description changes |
| **Who can add members** | All members / Only admins | All members | Controls invitation permissions |
| **Disappearing messages** | Off / 24h / 7d / 90d | Off | Auto-delete timer for messages |
| **Message approval** | On / Off | Off | [INFERRED] Admins approve messages before broadcast |

### Member Capacity

- **Maximum: 1,024 members per group** (WhatsApp increased this from 256 to 512 in 2022, then to 1,024 in 2023)
- This limit is not arbitrary — it is driven by fan-out cost, encryption overhead, and spam/privacy concerns (see [Section 7](#7-group-size-evolution))

### Communities Feature

WhatsApp introduced **Communities** as a layer above groups:

```
Community (meta-group)
├── Announcement Group (broadcast, admins only)
├── Sub-Group A (up to 1,024 members)
├── Sub-Group B (up to 1,024 members)
└── Sub-Group C (up to 1,024 members)
```

- A Community can contain up to **50 groups** with up to **5,000 total members** [UNVERIFIED — check official sources]
- The **Announcement Group** is admin-broadcast-only — used for org-wide messages
- Individual sub-groups function as normal groups
- This is WhatsApp's workaround for the 1,024-member limit while keeping fan-out bounded per group

### Data Model

```
Group {
    groupId:          UUID
    name:             String (max 100 chars)
    description:      String (max 2048 chars)
    photoUrl:         String (media reference)
    createdBy:        userId
    createdAt:        Timestamp
    settings:         GroupSettings
    memberCount:      Integer
}

GroupMembership {
    groupId:          UUID
    userId:           UUID
    role:             ADMIN | MEMBER
    joinedAt:         Timestamp
    addedBy:          userId
}
```

---

## 2. Fan-Out Strategies

This is the core architectural decision for group messaging. When one user sends a message to a group of N members, how does the system deliver it to all N-1 other members?

### Strategy A: Fan-Out on Write

```
Sender sends 1 message
         |
         v
   ┌──────────┐
   │  Server   │
   └──────────┘
         |
    Write 1 copy to EACH member's inbox
         |
    ┌────┼────┬────┬────┐
    v    v    v    v    v
  ┌───┐┌───┐┌───┐┌───┐┌───┐
  │M1 ││M2 ││M3 ││M4 ││...│  (N inboxes)
  │box││box││box││box││   │
  └───┘└───┘└───┘└───┘└───┘
    |    |    |    |    |
    v    v    v    v    v
  Each member reads from OWN inbox (fast!)
```

**How it works:**
1. Sender sends message to server
2. Server writes one copy of the message into each member's personal inbox/queue
3. Each member reads from their own inbox — same as reading a 1:1 message
4. No difference in read path between 1:1 and group messages

**This is WhatsApp's approach.** WhatsApp groups are capped at 1,024 members, so the write amplification is bounded.

**Pros:**
- **Fast reads**: Each member reads from their own inbox — O(1) per member, no joins, no shared state
- **Simple client logic**: Client doesn't distinguish between 1:1 and group messages during reads
- **Efficient offline delivery**: When a member comes online, all their messages (1:1 and group) are in one queue
- **Natural per-member delivery tracking**: Each inbox entry can have its own delivery/read status

**Cons:**
- **Write amplification**: 1 message becomes N writes (1,024 in the worst case)
- **Storage amplification**: N copies of the same message stored (mitigated by pointer/reference semantics)
- **Latency on send path**: Server must complete N writes before confirming delivery (can be async)

### Strategy B: Fan-Out on Read

```
Sender sends 1 message
         |
         v
   ┌──────────┐
   │  Server   │
   └──────────┘
         |
    Write 1 copy to GROUP LOG
         |
         v
   ┌──────────────┐
   │  Group Log    │
   │  (single log) │
   └──────────────┘
    ^    ^    ^    ^    ^
    |    |    |    |    |
  ┌───┐┌───┐┌───┐┌───┐┌───┐
  │M1 ││M2 ││M3 ││M4 ││...│  Each member reads from group log
  └───┘└───┘└───┘└───┘└───┘
```

**How it works:**
1. Sender sends message to server
2. Server writes ONE copy to the group's message log
3. Each member reads from the shared group log, tracking their own read cursor
4. Each member maintains: `(groupId, lastReadSequenceNumber)` as their cursor

**This is Discord's and Telegram's approach** for large groups/channels.

**Pros:**
- **Minimal write amplification**: 1 message = 1 write, regardless of group size
- **Minimal storage**: One copy per message, not N copies
- **Scales to millions of members**: Write cost is O(1), not O(N)

**Cons:**
- **Read amplification**: Every member reads from the same log — hot partition problem
- **Complex sync logic**: Client must track per-group cursors, handle gaps, merge group messages into a unified timeline
- **Read contention**: Millions of members reading from the same partition creates a read hotspot
- **Offline catch-up is harder**: Must query each group's log separately, then merge and sort

### Strategy C: Hybrid Approach

```
                    Message arrives for group
                             |
                             v
                    ┌─────────────────┐
                    │ Group size < 100?│
                    └─────────────────┘
                      /            \
                   Yes              No
                    /                \
                   v                  v
          ┌──────────────┐   ┌──────────────┐
          │ Fan-out on   │   │ Fan-out on   │
          │ WRITE        │   │ READ         │
          │ (copy to     │   │ (single      │
          │  each inbox) │   │  group log)  │
          └──────────────┘   └──────────────┘
```

**How it works:**
- **Small groups (< 100 members)**: Fan-out on write. Write amplification is small (< 100 writes), reads are fast.
- **Large groups (>= 100 members)**: Fan-out on read. Avoids massive write amplification, accepts more complex reads.
- The threshold (e.g., 100) is tunable based on system metrics.

### Decision Matrix

| Factor | Fan-Out on Write | Fan-Out on Read | Hybrid |
|--------|-----------------|----------------|--------|
| **Write cost per message** | O(N) — N writes | O(1) — 1 write | O(N) for small, O(1) for large |
| **Read cost per member** | O(1) — own inbox | O(1) per query but hot partition | Depends on group size |
| **Storage** | N copies per message | 1 copy per message | Mixed |
| **Max practical group size** | ~1,000-10,000 | Millions | Unlimited |
| **Client complexity** | Low (reads from own inbox) | High (per-group cursors, merging) | Medium |
| **Offline catch-up** | Simple (drain inbox) | Complex (query each group) | Mixed |
| **Delivery tracking** | Natural (per-inbox status) | Requires separate tracking | Mixed |
| **Best for** | WhatsApp (small groups) | Discord (huge servers) | General-purpose |

### Quantitative Analysis

Let's do the math for a worst-case WhatsApp group:

**Scenario:** 1,024-member group, 100 messages per minute (active group chat)

#### Fan-Out on Write

```
Messages per minute:                  100
Members per group:                    1,024
Writes per minute:                    100 × 1,024 = 102,400
Writes per second:                    102,400 / 60 ≈ 1,707

Storage per message (pointer):        ~200 bytes (messageId + metadata, actual payload stored once)
Storage per minute:                   102,400 × 200 B = ~20.5 MB/min
Storage per hour:                     ~1.2 GB/hour (but messages drained after delivery)
```

At WhatsApp scale — 100 billion messages/day, even if 10% are group messages:

```
Group messages per day:               10 billion
Average group size:                   ~20 (most groups are small)
Average fan-out:                      ~20
Total writes from group fan-out:      10B × 20 = 200 billion writes/day
Writes per second:                    200B / 86,400 ≈ 2.3 million writes/sec
```

This is substantial but manageable with a partitioned message store (Cassandra), because:
- Writes are distributed across all member partitions (no hotspot)
- Each write is small (pointer + metadata, not full message payload)
- Messages are deleted after delivery ACK, so steady-state storage is bounded

#### Fan-Out on Read

```
Messages per minute:                  100
Writes per minute:                    100 (one write per message)
Reads per minute (all members):       1,024 members × polling or push-triggered reads

If all members are online and receiving pushes:
  Server sends 100 push notifications × 1,024 = 102,400 pushes/min
  (Same delivery cost — the fan-out just moved from write path to push path)

If members check history (offline catch-up):
  Each member queries group log independently
  1,024 concurrent readers on same partition = read hotspot
```

**Key insight:** Fan-out on read does NOT eliminate fan-out — it moves it from the write path to the read/push path. The total work is the same. The question is: where do you want to pay the cost?

#### Why WhatsApp Chose Fan-Out on Write

```
Fan-out on write advantages for WhatsApp:
  1. Group size bounded at 1,024 — write amplification is capped
  2. Read path is the latency-critical path (users scrolling chat)
  3. Write can be async (user doesn't wait for all 1,024 writes)
  4. Matches 1:1 message delivery path — less code, fewer bugs
  5. Per-member delivery tracking falls out naturally
  6. Offline queue is unified — no need to merge multiple group logs

Fan-out on read advantages for Discord:
  1. Servers can have millions of members — write fan-out would be catastrophic
  2. Most members don't read most messages (lurkers)
  3. Messages are persistent (not deleted after delivery)
  4. Channel-based model — users read specific channels, not a unified inbox
```

---

## 3. Group Message Delivery

### End-to-End Flow

```
Sender                 Server                    Member Gateway        Member
  |                      |                            |                  |
  |-- Send group msg --->|                            |                  |
  |   (encrypted with    |                            |                  |
  |    sender key)       |                            |                  |
  |                      |-- Store message             |                  |
  |                      |-- Look up group members     |                  |
  |                      |                            |                  |
  |                      |-- For each member:          |                  |
  |                      |   ┌─────────────────────┐  |                  |
  |                      |   │ Is member online?    │  |                  |
  |                      |   └─────────────────────┘  |                  |
  |                      |     |              |       |                  |
  |                      |    Yes            No       |                  |
  |                      |     |              |       |                  |
  |                      |     v              v       |                  |
  |                      |   Route to      Store in   |                  |
  |                      |   gateway       offline    |                  |
  |                      |     |           queue      |                  |
  |                      |     |              |       |                  |
  |                      |     |-- Push msg --------->|-- Deliver ------>|
  |                      |     |              |       |                  |
  |                      |     |              |       |<-- ACK ----------|
  |                      |     |              |       |                  |
  |<-- Server ACK -------|     |              |       |                  |
  |   (msg accepted,     |     |              |       |                  |
  |    delivery async)   |     |              |       |                  |
```

### Per-Member Delivery Tracking

Each group message has N delivery states — one per member:

```
GroupMessageDelivery {
    messageId:    UUID
    groupId:      UUID
    memberId:     UUID
    status:       SENT | DELIVERED | READ
    deliveredAt:  Timestamp (nullable)
    readAt:       Timestamp (nullable)
}
```

**Read receipts in groups:**
- WhatsApp shows read receipts per-member in groups (long-press on a message to see who read it)
- This requires tracking delivery/read status for every (message, member) pair
- For a 1,024-member group with 100 messages: 102,400 status records
- Status updates are batched and eventually consistent — slight delays are acceptable

### Delivery States

```
Message lifecycle per member:

  SENT ──────> DELIVERED ──────> READ
   |               |                |
   |               |                |
   Msg accepted    Msg arrived on   Member opened
   by server       member's device  the chat and
                   (ACK received)   saw the message
```

The sender's UI aggregates these states:

- **Single check**: Message sent to server
- **Double check**: Message delivered to at least one recipient [INFERRED]
- **Blue double check**: Message read by at least one recipient (or all, depending on UI logic)

### Optimization: Batched Fan-Out

For large groups, the server doesn't fan out one-by-one. Instead:

```
1. Group message arrives
2. Fetch group membership list (cached in Redis/memcached)
3. Partition members by gateway server:
     Gateway A: [user1, user2, user5, user8, ...]
     Gateway B: [user3, user4, user6, ...]
     Gateway C: [user7, user9, user10, ...]
4. Send ONE batch message to each gateway server
5. Each gateway handles local delivery to its connected users
6. Non-connected users → offline queue (bulk insert)
```

This reduces network hops from O(N) to O(G) where G = number of gateway servers with at least one group member connected.

---

## 4. Group E2E Encryption with Sender Keys

### The Problem with Pairwise Encryption in Groups

In 1:1 chats, WhatsApp uses the Signal Protocol's **Double Ratchet** — each message is encrypted specifically for the recipient. In a group of N members, naively applying pairwise encryption means:

```
Pairwise approach (NOT used for groups):
  - Sender encrypts message N-1 times (once per recipient)
  - Each encryption uses a different key (pairwise session key)
  - For N=1,024: sender must perform 1,023 encryptions per message
  - O(N) encryption cost per message — too expensive
```

### Sender Keys: O(1) Encryption

WhatsApp uses the **Sender Keys** variant of the Signal Protocol for groups:

```
Setup (one-time per member per group):
┌─────────┐                              ┌─────────┐
│ Alice    │                              │  Bob    │
│ (member) │                              │(member) │
└─────────┘                              └─────────┘
     |                                        |
     |-- Generate Alice's Sender Key          |-- Generate Bob's Sender Key
     |                                        |
     |-- Distribute Alice's Sender Key ------>|  (via pairwise E2E channel)
     |                                        |
     |<-- Receive Bob's Sender Key -----------|  (via pairwise E2E channel)
     |                                        |

Sending a message:
┌─────────┐         ┌─────────┐         ┌─────────┐
│ Alice    │         │ Server  │         │  Bob    │
└─────────┘         └─────────┘         └─────────┘
     |                    |                    |
     |-- Encrypt with     |                    |
     |   Alice's Sender   |                    |
     |   Key (O(1))       |                    |
     |                    |                    |
     |-- Send encrypted ->|                    |
     |   blob             |                    |
     |                    |-- Fan-out -------->|
     |                    |   (same blob       |
     |                    |    to all members) |
     |                    |                    |
     |                    |         Decrypt with Alice's
     |                    |         Sender Key (O(1))
```

**How Sender Keys work:**
1. Each group member generates a **sender key** (symmetric key + chain key for ratcheting)
2. The sender key is distributed to every other group member via their existing **pairwise E2E channel** (Double Ratchet session)
3. When Alice sends a group message, she encrypts it **once** with her sender key
4. The server fans out the **same encrypted blob** to all members
5. Each member decrypts using Alice's sender key (which they received during setup)

**Cost comparison:**

| Operation | Pairwise (Double Ratchet) | Sender Keys |
|-----------|--------------------------|-------------|
| Encryptions per send | O(N) — one per member | O(1) — one encryption |
| Key distribution (setup) | None (already have sessions) | O(N) — distribute to each member via pairwise |
| Decryptions per receive | O(1) | O(1) |
| Key rotation on membership change | N/A | O(N) — redistribute new sender key |

### Key Rotation on Membership Changes

When a member is **added** or **removed**, sender keys must be rotated to preserve security:

```
Member removed (e.g., Carol kicked from group):

  Before: Alice, Bob, Carol all have each other's sender keys

  After Carol removed:
    1. Alice generates NEW sender key
    2. Alice distributes new key to Bob (via pairwise channel)
       (Alice does NOT send new key to Carol)
    3. Bob generates NEW sender key
    4. Bob distributes new key to Alice (via pairwise channel)

  Result: Carol has the OLD sender keys but NOT the new ones
          Future messages encrypted with new keys are unreadable by Carol

Member added (e.g., Dave joins):
    1. Each existing member generates a NEW sender key
    2. Each member distributes their new key to ALL members (including Dave)
    3. Dave generates his sender key and distributes to all

  Result: Dave has everyone's NEW sender keys
          Dave does NOT have old keys — cannot read message history
```

### Forward Secrecy Trade-Off

| Property | 1:1 (Double Ratchet) | Group (Sender Keys) |
|----------|---------------------|---------------------|
| **Forward secrecy** | Per-message (DH ratchet on each turn) | Per-membership-change (key rotation) |
| **Compromise impact** | Compromised key reveals only that message | Compromised sender key reveals all messages from that sender until next key rotation |
| **Post-compromise recovery** | Next DH ratchet step restores security | Next membership change forces key rotation |

**Why this trade-off is acceptable:**
- Sender Keys reduce encryption from O(N) to O(1) per message — critical for 1,024-member groups
- Key rotation happens on every membership change, limiting the window of compromise
- The alternative (O(N) pairwise encryption) would make large groups impractical on mobile devices
- WhatsApp's threat model assumes the server is untrusted but members are semi-trusted (you chose to be in the group)

---

## 5. Ordering in Groups

### Single Monotonic Sequence Number Per Group

```
Group "Engineering Team" (groupId: grp_42)

  Alice sends "Hello"     →  Server assigns seqNo = 1
  Bob sends "Hi Alice"    →  Server assigns seqNo = 2
  Alice sends "Meeting?"  →  Server assigns seqNo = 3
  Carol sends "Sure"      →  Server assigns seqNo = 4

All members see:
  [1] Alice: Hello
  [2] Bob: Hi Alice
  [3] Alice: Meeting?
  [4] Carol: Sure
```

**How it works:**
- The server maintains a **single atomic counter** per group
- When a group message arrives, the server assigns the next sequence number
- All members receive messages with the same sequence numbers
- The client displays messages in sequence number order

**Implementation:**

```
-- Atomic sequence assignment (pseudo-SQL)
BEGIN TRANSACTION;
  SELECT next_seq FROM group_sequences WHERE group_id = ? FOR UPDATE;
  UPDATE group_sequences SET next_seq = next_seq + 1 WHERE group_id = ?;
  INSERT INTO group_messages (group_id, seq_no, ...) VALUES (?, next_seq, ...);
COMMIT;
```

Or with Redis:

```
seq = INCR group:{groupId}:seq
```

### Why This Is Simpler Than Causal Ordering

**Causal ordering** (using vector clocks or Lamport timestamps) tracks "happened-before" relationships:

```
Causal ordering complexity:
  - Each member maintains a vector clock of size N (number of members)
  - On send: increment own clock entry
  - On receive: merge clocks (element-wise max)
  - Ordering: message A < message B iff A's clock < B's clock componentwise
  - For N=1,024: each message carries a vector of 1,024 integers
  - Concurrent messages may have no defined order (partial order, not total)
```

**Why causal ordering is overkill for chat:**

| Property | Server-assigned seq no | Causal ordering (vector clocks) |
|----------|----------------------|--------------------------------|
| **Ordering** | Total order (all messages have a definitive position) | Partial order (some messages are concurrent/unordered) |
| **Metadata overhead** | 1 integer per message | N integers per message (N = member count) |
| **Complexity** | Trivial (atomic increment) | Vector clock merge on every message |
| **Consistency** | All members see the same order always | Members may see different valid orderings of concurrent messages |
| **UX** | Deterministic — everyone sees the same chat | Possible confusion — different members see different orderings |

**The key insight:** Chat UX requires a **total order** — users expect a single definitive order for all messages. Server-assigned sequence numbers give this trivially. Causal ordering gives a partial order which must then be arbitrarily extended to a total order anyway, adding complexity for no UX benefit.

**Trade-off:** Server-assigned ordering means the server determines order, not the sending clients. If two messages are sent simultaneously, the server (not the causal relationship) determines which comes first. In practice, for chat messages arriving within milliseconds of each other, the arbitrary server-assigned order is indistinguishable from any "correct" causal order.

### Gap Detection and Re-Delivery

Clients use sequence numbers to detect missing messages:

```
Client receives: seqNo = 5, then seqNo = 8
  → Gap detected: missing seqNo 6 and 7
  → Request re-delivery: GET /groups/{groupId}/messages?from=6&to=7
  → Server delivers missing messages
  → Client inserts them in correct position
```

---

## 6. Admin Controls

### Role Model

```
┌──────────────────────────────────────────────┐
│                  Group                        │
│                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐│
│  │  CREATOR  │   │  ADMIN   │   │  MEMBER  ││
│  │  (admin   │   │          │   │          ││
│  │  by       │   │          │   │          ││
│  │  default) │   │          │   │          ││
│  └──────────┘   └──────────┘   └──────────┘│
│                                              │
│  Permissions:                                │
│  Creator ⊇ Admin ⊇ Member                   │
└──────────────────────────────────────────────┘
```

### Permission Matrix

| Operation | Creator | Admin | Member |
|-----------|:-------:|:-----:|:------:|
| Send message (when "all can send") | Yes | Yes | Yes |
| Send message (when "admins only") | Yes | Yes | No |
| Read messages | Yes | Yes | Yes |
| Add members (when "all can add") | Yes | Yes | Yes |
| Add members (when "admins only") | Yes | Yes | No |
| Remove members | Yes | Yes | No |
| Change group name/photo/desc | Depends on setting | Depends on setting | Depends on setting |
| Change group settings | Yes | Yes | No |
| Promote member to admin | Yes | Yes | No |
| Demote admin to member | Yes | Yes* | No |
| Delete group | Yes | No | No |

*An admin can demote another admin but typically cannot demote the creator [INFERRED].

### Metadata Operations vs Message Operations

Admin actions fall into two distinct categories that are processed differently:

**Metadata operations** (admin controls):

```
- Add/remove member
- Change group name, photo, description
- Promote/demote admin
- Change group settings
- These generate "system messages" visible in chat:
    "Alice added Bob to the group"
    "Carol changed the group name to 'Project X'"
- Processed synchronously (must be consistent before next message)
- Triggers sender key rotation (for add/remove)
```

**Message operations** (chat messages):

```
- Send text, media, location, contact
- Processed asynchronously (fan-out can be eventual)
- Subject to "who can send" permission check
- E2E encrypted with sender keys
```

**Why the distinction matters:**
- Metadata operations modify group state (membership, settings) — they must be strongly consistent. All members must see the same membership list before new messages are sent.
- Message operations are the high-throughput path — they can tolerate eventual consistency in delivery order.
- A membership change MUST be ordered before any subsequent messages encrypted with the new sender keys. Otherwise, a member might receive a message encrypted with a key they don't yet have.

### "Only Admins Can Send" Mode

This effectively turns the group into a **broadcast channel**:

```
Normal group:                   Admin-only send:
  Alice (admin): "Hello"          Alice (admin): "Announcement"
  Bob (member): "Hi"              Bob (member): [BLOCKED]
  Carol (member): "Hey"           Carol (member): [BLOCKED]
  Dave (admin): "Team call"       Dave (admin): "Update: ..."
```

Use cases: School announcements, company communications, community bulletins.

Server enforcement:

```
on_message_received(groupId, senderId, message):
    group = get_group(groupId)
    if group.settings.send_permission == ADMINS_ONLY:
        if get_member_role(groupId, senderId) != ADMIN:
            return Error(403, "Only admins can send messages in this group")
    # proceed with fan-out
```

---

## 7. Group Size Evolution

### WhatsApp's Group Size History

| Year | Max Members | Change |
|------|------------|--------|
| ~2013-2016 | 100 | Initial limit |
| ~2016-2020 | 256 | Doubled |
| 2022 | 512 | Doubled again |
| 2023 | 1,024 | Current limit |

### Why There's a Limit

**1. Fan-out cost (write amplification):**

```
Group size:     1,024 members
Messages/min:   100 (active group)
Fan-out writes: 100 × 1,024 = 102,400 writes/min

If raised to 10,000:
Fan-out writes: 100 × 10,000 = 1,000,000 writes/min (per group!)
```

Each doubling of the group size limit doubles the worst-case write amplification. WhatsApp's fan-out-on-write model puts a hard ceiling on practical group size.

**2. Encryption overhead (sender key distribution):**

```
Adding 1 new member to a 1,024-member group:
  - 1,024 existing members must each generate and distribute a new sender key
  - Each distribution goes through a pairwise E2E channel
  - Total pairwise messages: 1,024 × 1,024 = ~1 million key exchange messages

Adding 1 new member to a 10,000-member group:
  - 10,000 × 10,000 = 100 million key exchange messages
  - This is O(N^2) and becomes impractical
```

**3. Privacy concerns:**

```
Large group = higher risk of:
  - Unknown members seeing your messages
  - Phone number exposure (WhatsApp uses phone numbers as identity)
  - Screenshots and forwarding of private messages
  - Spam and abuse (harder to moderate large groups)
```

**4. Spam and abuse:**

```
Spam reach with 1 message:
  256-member group:   reaches 255 people
  1,024-member group: reaches 1,023 people
  10,000-member group: reaches 9,999 people (broadcast spam)

WhatsApp has been used for misinformation in large groups
  → Limiting group size is a deliberate anti-misinformation measure
```

### Communities as a Workaround

Instead of raising the group limit further, WhatsApp introduced Communities:

```
Without Communities:                  With Communities:
┌──────────────────────┐             ┌──────────────────────────────┐
│ Giant Group (10K?)   │             │ Community                    │
│ - All 10K in one     │             │ ┌────────────────────────┐  │
│   fan-out            │             │ │ Announcement (all 5K)  │  │
│ - O(N^2) key         │             │ │ (admin broadcast only) │  │
│   distribution       │             │ └────────────────────────┘  │
│ - Unmanageable       │             │ ┌──────┐ ┌──────┐ ┌──────┐ │
│   noise              │             │ │Grp A │ │Grp B │ │Grp C │ │
└──────────────────────┘             │ │ 200  │ │ 500  │ │ 300  │ │
                                     │ │ mbrs │ │ mbrs │ │ mbrs │ │
                                     │ └──────┘ └──────┘ └──────┘ │
                                     └──────────────────────────────┘

Benefits:
  - Each sub-group has bounded fan-out (≤1,024)
  - Announcement group is admin-only (no N-way fan-out of member messages)
  - Users self-select relevant sub-groups (reduces noise)
  - Each sub-group has its own sender keys (bounded O(N^2))
```

---

## 8. Contrast with Discord Servers

Discord's group messaging model is fundamentally different from WhatsApp's. The architectural choices diverge because the products serve different purposes.

### Structural Differences

```
WhatsApp Group:                      Discord Server:
┌──────────────────┐                ┌──────────────────────────────┐
│ Group             │                │ Server                       │
│ (up to 1,024)    │                │ (up to millions of members)  │
│                  │                │                              │
│ Single message   │                │ ┌──────────┐ ┌──────────┐  │
│ stream           │                │ │#general  │ │#random   │  │
│                  │                │ │(channel) │ │(channel) │  │
│ All members see  │                │ └──────────┘ └──────────┘  │
│ all messages     │                │ ┌──────────┐ ┌──────────┐  │
│                  │                │ │#dev      │ │#voice-1  │  │
└──────────────────┘                │ │(channel) │ │(voice ch)│  │
                                    │ └──────────┘ └──────────┘  │
                                    │                              │
                                    │ Roles: Owner, Admin, Mod,    │
                                    │ @everyone, custom roles...   │
                                    └──────────────────────────────┘
```

### Architectural Comparison

| Aspect | WhatsApp Groups | Discord Servers |
|--------|----------------|----------------|
| **Max members** | 1,024 | ~500,000 (some servers reach millions) |
| **Fan-out strategy** | Write (copy to each inbox) | Read (shared channel log) |
| **Message persistence** | Transient — deleted from server after delivery | Permanent — all messages stored indefinitely |
| **Encryption** | E2E (sender keys) — server cannot read | Transport encryption only — server stores plaintext |
| **Channels** | None — single message stream | Multiple channels within a server |
| **Roles/Permissions** | Admin / Member (simple) | Complex role hierarchy with per-channel permission overrides |
| **Voice** | Separate call feature | Persistent voice channels (always-on rooms) |
| **Message search** | Client-side only (E2E prevents server search) | Server-side full-text search |
| **Identity** | Phone number | Username + discriminator (now just username) |
| **Tech stack** | Erlang/BEAM | Elixir/BEAM (same VM, different language) |

### Why Discord Can't Use Fan-Out on Write

```
Discord server: "Minecraft" — 1,000,000 members
Member sends "gg" in #general

Fan-out on write:
  1,000,000 inbox writes per message
  × 100 messages/min in an active channel
  = 100,000,000 writes/min
  = 1.67 million writes/sec (from ONE channel in ONE server)

  → IMPOSSIBLE at Discord's scale

Fan-out on read (Discord's approach):
  1 write per message to channel log
  Only members who OPEN #general read from the log
  Most members are lurkers who never open most channels
  Effective read load << 1,000,000 (maybe 1-5% active readers)
```

### Why WhatsApp Can't Use Fan-Out on Read

```
WhatsApp: every message must be DELIVERED to every member
  - Messages are E2E encrypted — client must receive and decrypt
  - No server-side storage (transient relay)
  - Read receipts require per-member delivery tracking
  - Offline members need messages queued and delivered later

Discord: messages are available ON DEMAND
  - Messages stored permanently on server
  - Members read when they choose to open a channel
  - No per-member delivery tracking (no read receipts per message)
  - "Unread" is just a cursor position, not queued delivery
```

---

## 9. Contrast with Telegram Groups/Channels

### Telegram's Group/Channel Model

```
Telegram Groups:                     Telegram Channels:
┌──────────────────────┐            ┌──────────────────────┐
│ Group                │            │ Channel              │
│ Up to 200,000 members│            │ UNLIMITED subscribers│
│                      │            │                      │
│ All members can send │            │ Only admins can post │
│ (unless restricted)  │            │ (broadcast only)     │
│                      │            │                      │
│ Cloud-stored         │            │ Cloud-stored         │
│ Searchable history   │            │ Searchable history   │
│ Accessible from any  │            │ Accessible from any  │
│ device               │            │ device               │
└──────────────────────┘            └──────────────────────┘
```

### Architectural Comparison

| Aspect | WhatsApp Groups | Telegram Groups | Telegram Channels |
|--------|----------------|----------------|-------------------|
| **Max members** | 1,024 | 200,000 | Unlimited |
| **Message storage** | Transient (deleted after delivery) | Permanent cloud storage | Permanent cloud storage |
| **E2E encryption** | Always (sender keys) | No (client-server only); E2E only in Secret Chats (1:1) | No |
| **Message history** | On-device only; new device = no history | Full history from cloud on any device | Full history from cloud on any device |
| **Search** | Client-side (limited) | Server-side full-text search | Server-side full-text search |
| **Fan-out** | On write (bounded by 1,024 limit) | On read (stored once, read from cloud) [INFERRED] | On read (broadcast, subscribers pull) |
| **File sharing** | Compressed, E2E encrypted, limited retention | Up to 2 GB per file, cloud-stored, no size limit retention | Same as groups |
| **Bot platform** | Limited (WhatsApp Business API) | Rich bot API (inline bots, keyboards, commands) | Same as groups |

### Why Telegram Can Support 200K Members

Telegram's architectural choices enable massive groups:

```
1. No E2E encryption (regular chats):
   - Server stores plaintext → can index, search, deduplicate
   - No sender key distribution overhead (O(N^2) problem disappears)
   - Server can do fan-out on read (members pull from cloud log)

2. Cloud storage model:
   - Messages stored permanently on Telegram's servers
   - Members read from shared cloud log (no per-member inbox)
   - No offline queue needed — messages are always in the cloud

3. No per-member delivery tracking:
   - Telegram doesn't show individual read receipts in large groups
   - No need to track delivery status for 200K members per message

4. Fan-out on read:
   - Message written once to cloud
   - Members read when they open the chat
   - Most members in a 200K group are lurkers — never read most messages
```

### The Privacy vs Convenience Trade-Off

```
WhatsApp:                              Telegram:
┌─────────────────────┐               ┌─────────────────────┐
│ PRIVACY-FIRST       │               │ CONVENIENCE-FIRST   │
│                     │               │                     │
│ E2E encrypted       │               │ Cloud-stored        │
│ Server = dumb relay │               │ Server = smart hub  │
│ History on-device   │               │ History anywhere    │
│ Small groups (1024) │               │ Huge groups (200K)  │
│ No server search    │               │ Full-text search    │
│ Media expires       │               │ Media permanent     │
│ Limited bots        │               │ Rich bot platform   │
│                     │               │                     │
│ Trade-off:          │               │ Trade-off:          │
│ Less convenient     │               │ Less private        │
│ but more private    │               │ but more convenient │
└─────────────────────┘               └─────────────────────┘
```

---

## 10. Contrast with Slack Channels

### Slack's Channel Model

```
Slack Workspace:
┌──────────────────────────────────────────────┐
│ Workspace ("Acme Corp")                      │
│                                              │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐     │
│ │#general  │ │#eng      │ │#random   │     │
│ │(public)  │ │(private) │ │(public)  │     │
│ └──────────┘ └──────────┘ └──────────┘     │
│                                              │
│ ┌──────────┐ ┌──────────────────────┐       │
│ │#proj-x   │ │  Thread in #eng     │       │
│ │(private) │ │  ├── Original msg   │       │
│ └──────────┘ │  ├── Reply 1        │       │
│              │  ├── Reply 2        │       │
│              │  └── Reply 3        │       │
│              └──────────────────────┘       │
│                                              │
│ Apps & Integrations:                         │
│ [Jira] [GitHub] [PagerDuty] [Custom Bot]     │
└──────────────────────────────────────────────┘
```

### Architectural Comparison

| Aspect | WhatsApp Groups | Slack Channels |
|--------|----------------|----------------|
| **Scope** | Phone-number-based, global | Workspace-scoped (org/company) |
| **Identity** | Phone number | Email (enterprise SSO) |
| **Max members** | 1,024 | Workspace limit (varies by plan; large orgs have tens of thousands) |
| **Encryption** | E2E (server cannot read) | Transport encryption; server stores plaintext (enterprise compliance) |
| **Message persistence** | Transient | Permanent (searchable, exportable, subject to retention policies) |
| **Threads** | No native threading | Core feature — threaded conversations |
| **Integrations** | Limited (Business API) | Rich ecosystem: 2,600+ apps, custom bots, workflows |
| **Search** | Client-side only | Server-side full-text search across all channels |
| **Compliance** | Minimal (E2E limits server-side compliance) | eDiscovery, DLP, audit logs, retention policies |
| **Message format** | Text + media | Rich text (Markdown), code blocks, attachments, interactive blocks |
| **Channels** | None (single stream) | Public, private, shared (cross-workspace) |
| **Fan-out** | On write | On read [INFERRED] — messages stored in channel, read on demand |

### Why Slack's Architecture Differs

Slack serves **enterprise** customers with fundamentally different requirements:

```
Enterprise requirements:
  1. Compliance: Admins must be able to audit, search, and export all messages
     → E2E encryption is incompatible with this requirement
     → Server MUST store plaintext for eDiscovery/DLP

  2. Persistence: Knowledge retention — old messages are a searchable archive
     → Transient relay model (like WhatsApp) would lose institutional knowledge

  3. Integrations: Bots read and write messages programmatically
     → E2E encryption would prevent bot access to message content

  4. Threaded conversations: Organize discussions within a channel
     → Threads require server-side awareness of message relationships
     → Cannot be done with dumb relay

  5. Cross-workspace channels: Shared channels between organizations
     → Requires server-mediated access control (not just member lists)
```

### Slack's Storage Model

```
WhatsApp message lifecycle:          Slack message lifecycle:
  Send → Encrypt → Server relay      Send → Server stores (plaintext)
  → Deliver → ACK → DELETE           → Index for search
                                     → Retain per policy (30d, 90d, forever)
                                     → Available for compliance export
                                     → Accessible from any device, any time
```

Slack uses MySQL (via Vitess for sharding) for message storage — ACID transactions, strong consistency, full-text indexing. This is the opposite of WhatsApp's eventually-consistent, high-throughput, transient message store.

---

## Summary: Group Messaging Decision Tree

```
                    How big is the group?
                           |
              ┌────────────┼────────────┐
              |            |            |
          Small          Medium        Large
        (< 100)       (100-1,024)   (> 1,024)
              |            |            |
              v            v            v
         Fan-out       Fan-out      Fan-out
         on write      on write     on read
         (simple)      (bounded)    (necessary)
              |            |            |
              v            v            v
         WhatsApp      WhatsApp     Discord /
         default       max          Telegram
                                       |
                                       v
                              ┌─────────────────┐
                              │ E2E encryption?  │
                              └─────────────────┘
                                /            \
                             Yes              No
                              /                \
                             v                  v
                     Not practical        Server stores
                     at this scale       plaintext — can
                     (O(N^2) key         index, search,
                     distribution)       moderate
                              |                |
                              v                v
                     WhatsApp chose       Telegram, Discord,
                     to CAP group        Slack chose to
                     size at 1,024       sacrifice E2E for
                     to preserve E2E     scale/features
```

---

## References

- [WhatsApp E2E Encryption White Paper](https://www.whatsapp.com/security/WhatsApp-Security-Whitepaper.pdf)
- [Signal Protocol: Sender Keys](https://signal.org/docs/specifications/group-v2/)
- See also: `05-end-to-end-encryption.md` for full Signal Protocol deep dive
- See also: `03-messaging-and-delivery.md` for 1:1 delivery pipeline
- See also: `06-storage-and-data-model.md` for data model details
