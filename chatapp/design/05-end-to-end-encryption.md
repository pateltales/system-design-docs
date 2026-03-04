# 05 — End-to-End Encryption (Signal Protocol)

> E2E encryption is WhatsApp's defining architectural constraint.
> The server is an **untrusted relay** — it NEVER sees plaintext messages.
> Every design decision in the system (storage, search, moderation, multi-device, backups) bends around this single fact.

---

## Table of Contents

1. [Signal Protocol Overview](#1-signal-protocol-overview)
2. [Key Hierarchy](#2-key-hierarchy)
3. [X3DH — Extended Triple Diffie-Hellman](#3-x3dh--extended-triple-diffie-hellman)
4. [Double Ratchet Algorithm](#4-double-ratchet-algorithm)
5. [Group E2E Encryption — Sender Keys](#5-group-e2e-encryption--sender-keys)
6. [Server's Role — The Untrusted Relay](#6-servers-role--the-untrusted-relay)
7. [Safety Numbers / Security Codes](#7-safety-numbers--security-codes)
8. [Multi-Device E2E](#8-multi-device-e2e)
9. [Encrypted Backups](#9-encrypted-backups)
10. [Architectural Implications — The Cost of Privacy](#10-architectural-implications--the-cost-of-privacy)
11. [Contrast with Telegram](#11-contrast-with-telegram)
12. [Contrast with Slack / Discord](#12-contrast-with-slack--discord)
13. [Interview Rubric — L5 / L6 / L7](#13-interview-rubric--l5--l6--l7)

---

## 1. Signal Protocol Overview

### Origin and Adoption

The Signal Protocol was developed by **Open Whisper Systems**, founded by **Moxie Marlinspike** and Trevor Perrin. Originally called the Axolotl Ratchet, it was renamed the Double Ratchet Algorithm and became the foundation of the Signal Protocol.

**Adopted by:**
- **Signal** — the reference implementation
- **WhatsApp** — deployed to 1+ billion users in 2016, making it the largest E2E deployment in history
- **Facebook Messenger** — optional "Secret Conversations" mode (now default for personal messages as of late 2023)
- **Google Messages** — RCS-based E2E encryption

### Security Properties

| Property | What It Means | How It's Achieved |
|---|---|---|
| **Forward Secrecy** | Compromise of long-term keys does NOT reveal past messages | Ephemeral keys via Double Ratchet; old keys are deleted |
| **Post-Compromise Security** | System recovers security after a key compromise | DH ratchet introduces new randomness on every turn change |
| **Deniability** | Cannot cryptographically prove who sent a message to a third party | No digital signatures on messages; both parties can forge transcripts |
| **Confidentiality** | Only sender and recipient can read messages | AES-256-CBC encryption with per-message keys |
| **Integrity** | Messages cannot be tampered with without detection | HMAC-SHA256 authentication on every message |

### Cryptographic Primitives

| Primitive | Algorithm | Purpose |
|---|---|---|
| Key Agreement | **Curve25519** (Elliptic Curve Diffie-Hellman) | All DH operations in X3DH and the DH ratchet |
| Symmetric Encryption | **AES-256-CBC** | Encrypting message payloads |
| Message Authentication | **HMAC-SHA256** | Authenticating encrypted messages, KDF chain derivation |
| Key Derivation | **HKDF** (HMAC-based Key Derivation Function) | Deriving root keys, chain keys, and message keys |
| Digital Signatures | **Ed25519** (EdDSA on Curve25519) | Signing pre-keys (identity key signs signed pre-key) |
| Hash | **SHA-256** / **SHA-512** | Safety number generation, various internal hashing |

> **Why Curve25519?** It provides 128-bit security with 32-byte keys, is resistant to timing attacks by design, and operations are fast on mobile hardware. It was designed by Daniel J. Bernstein specifically for high-performance, high-security DH key exchange.

---

## 2. Key Hierarchy

Every device in the system maintains a hierarchy of cryptographic keys. These keys serve different lifetimes and purposes, layering security properties on top of each other.

```
┌─────────────────────────────────────────────────────────────────┐
│                        KEY HIERARCHY                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌───────────────────────────────────────┐                      │
│  │       IDENTITY KEY PAIR (IK)          │  Long-term           │
│  │  Generated once at registration       │  Never changes       │
│  │  Curve25519 key pair                  │  Per device          │
│  │  Public part uploaded to server       │                      │
│  └──────────────────┬────────────────────┘                      │
│                     │ signs                                     │
│  ┌──────────────────▼────────────────────┐                      │
│  │     SIGNED PRE-KEY (SPK)              │  Medium-term         │
│  │  Curve25519 key pair + signature      │  Rotated weekly      │
│  │  Signed by Identity Key               │  1 active at a time  │
│  │  Public part + signature on server    │                      │
│  └──────────────────┬────────────────────┘                      │
│                     │                                           │
│  ┌──────────────────▼────────────────────┐                      │
│  │     ONE-TIME PRE-KEYS (OPK)           │  Ephemeral           │
│  │  Batch of 100+ Curve25519 key pairs   │  Each used once      │
│  │  Public parts uploaded to server      │  Consumed on use     │
│  │  Server deletes after dispensing      │  Client refills      │
│  └──────────────────┬────────────────────┘                      │
│                     │ X3DH derives                              │
│  ┌──────────────────▼────────────────────┐                      │
│  │     SESSION KEYS                      │  Per-session         │
│  │  Root Key (RK)                        │  Derived from X3DH   │
│  │  Chain Keys (CK_s, CK_r)             │  Ratcheted per msg   │
│  │  Message Keys (MK)                   │  Unique per message  │
│  └───────────────────────────────────────┘                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Key Details

**Identity Key Pair (IK)**
- Generated at device registration. One per device (not per account — this matters for multi-device).
- The private key never leaves the device.
- The public key is uploaded to the server and distributed in pre-key bundles.
- Used to sign the Signed Pre-Key and for X3DH DH computations.
- If the Identity Key changes (device reinstall, new device), the safety number changes — alerting contacts.

**Signed Pre-Key (SPK)**
- A medium-term Curve25519 key pair, rotated periodically (WhatsApp rotates approximately weekly).
- Signed by the Identity Key using Ed25519 — this lets other users verify the SPK belongs to the Identity Key holder.
- Only ONE active SPK at a time. Old SPKs are kept briefly for in-flight sessions, then deleted.
- If no one-time pre-keys are available, X3DH falls back to using only the SPK (weaker but still secure).

**One-Time Pre-Keys (OPK)**
- Ephemeral Curve25519 key pairs generated in batches (100+).
- Public parts are uploaded to the server.
- When Alice initiates a session with Bob, the server gives Alice one of Bob's OPKs and **deletes it** — it is consumed.
- If all OPKs are exhausted, X3DH proceeds without one (3 DH computations instead of 4). The client replenishes when the count drops below a threshold.
- Purpose: ensures each new session has unique keying material, even if two sessions are initiated simultaneously.

**Session Keys**
- Derived from the X3DH shared secret.
- **Root Key (RK)**: The long-lived key that seeds each new chain. Updated on every DH ratchet step.
- **Chain Keys (CK)**: Separate sending and receiving chains. Advanced once per message (symmetric ratchet).
- **Message Keys (MK)**: Derived from the chain key. Each message encrypted with a unique MK. Deleted after use (forward secrecy).

---

## 3. X3DH — Extended Triple Diffie-Hellman

X3DH is the **initial key agreement protocol** — it establishes a shared secret between two parties who have never communicated before. Critically, it works **asynchronously**: Bob does not need to be online when Alice initiates the session.

### Pre-Key Bundle

Before any session can be established, each user uploads a **pre-key bundle** to the server:

```
Bob's Pre-Key Bundle (stored on server):
┌──────────────────────────────────────┐
│  Identity Key (IK_B)     [public]    │
│  Signed Pre-Key (SPK_B)  [public]    │
│  SPK Signature           [Ed25519]   │
│  One-Time Pre-Key (OPK_B) [public]   │  ← one of the batch
└──────────────────────────────────────┘
```

### Protocol Flow

```
    ALICE                          SERVER                           BOB
      │                              │                               │
      │  1. "I want to message Bob"  │                               │
      ├─────────────────────────────►│                               │
      │                              │                               │
      │  2. Bob's Pre-Key Bundle     │                               │
      │     (IK_B, SPK_B, OPK_B)    │                               │
      │◄─────────────────────────────┤                               │
      │                              │                               │
      │  Server DELETES OPK_B        │                               │
      │  from Bob's stored bundle    │                               │
      │                              │                               │
      │  3. Alice performs 4 DH      │                               │
      │     computations locally:    │                               │
      │                              │                               │
      │  DH1 = DH(IK_A, SPK_B)      │    Mutual authentication      │
      │  DH2 = DH(EK_A, IK_B)       │    Mutual authentication      │
      │  DH3 = DH(EK_A, SPK_B)      │    Forward secrecy            │
      │  DH4 = DH(EK_A, OPK_B)      │    One-time uniqueness        │
      │                              │                               │
      │  EK_A = Alice's ephemeral    │                               │
      │         key (generated now)  │                               │
      │                              │                               │
      │  4. Shared Secret =          │                               │
      │     KDF(DH1 || DH2 ||       │                               │
      │         DH3 || DH4)          │                               │
      │                              │                               │
      │  5. Initialize Double        │                               │
      │     Ratchet with shared      │                               │
      │     secret as Root Key       │                               │
      │                              │                               │
      │  6. Encrypt first message    │                               │
      │     with Double Ratchet      │                               │
      │                              │                               │
      │  7. Send: {IK_A, EK_A,      │                               │
      │     OPK_B_id, ciphertext}    │                               │
      ├─────────────────────────────►│                               │
      │                              │  8. Store/forward to Bob      │
      │                              ├──────────────────────────────►│
      │                              │                               │
      │                              │  9. Bob receives message      │
      │                              │     Looks up own private keys │
      │                              │     Performs same 4 DH comps  │
      │                              │     Derives same shared secret│
      │                              │     Initializes Double Ratchet│
      │                              │     Decrypts message          │
      │                              │                               │
```

### Step-by-Step Breakdown

**Step 1-2**: Alice requests Bob's pre-key bundle from the server. The server returns the bundle and **deletes** the one-time pre-key it dispensed. This OPK is now consumed forever.

**Step 3**: Alice generates a fresh **ephemeral key pair** (EK_A) and performs four DH computations:

| DH Computation | Alice's Key | Bob's Key | Purpose |
|---|---|---|---|
| DH1 | IK_A (private) | SPK_B (public) | Authenticates Alice to Bob |
| DH2 | EK_A (private) | IK_B (public) | Authenticates Bob to Alice |
| DH3 | EK_A (private) | SPK_B (public) | Provides forward secrecy (ephemeral × medium-term) |
| DH4 | EK_A (private) | OPK_B (public) | Provides per-session uniqueness |

**Step 4**: The four DH outputs are concatenated and passed through HKDF to derive a shared secret `SK`:
```
SK = HKDF(DH1 || DH2 || DH3 || DH4)
```

**Step 5-6**: `SK` becomes the initial Root Key for the Double Ratchet. Alice encrypts her first message.

**Step 7**: Alice sends a header containing her Identity Key, Ephemeral Key, and the ID of the OPK she used, along with the ciphertext.

**Step 8-9**: Bob receives the message, retrieves his private keys for IK_B, SPK_B, and OPK_B, performs the same four DH computations (with roles reversed), derives the same shared secret, and decrypts.

### Fallback Without One-Time Pre-Key

If Bob has no remaining OPKs on the server, X3DH proceeds with only **3 DH computations** (DH1, DH2, DH3). The protocol is still secure but loses the per-session uniqueness guarantee. This is why clients should replenish OPKs proactively.

### Why "Extended Triple"?

The original Triple Diffie-Hellman (3DH) uses DH1, DH2, DH3. The "Extended" part is DH4 (the one-time pre-key), which provides additional protection against:
- **Key compromise impersonation**: Even if an attacker compromises Alice's identity key, they cannot impersonate Bob to Alice without the OPK.
- **Session correlation**: Each session uses a unique OPK, making sessions unlinkable.

### Why This Works Asynchronously

The key insight: Bob's pre-key bundle is uploaded **ahead of time**. Alice can establish a session and send an encrypted message **without Bob being online**. The server stores the encrypted message. When Bob comes online, he downloads the message header, retrieves his own private keys locally, and completes the handshake. No round-trip needed.

---

## 4. Double Ratchet Algorithm

After X3DH establishes the initial shared secret, the **Double Ratchet Algorithm** takes over for all subsequent message encryption. It combines two ratcheting mechanisms to provide forward secrecy and post-compromise recovery on a **per-message** basis.

### The Two Ratchets

```
┌─────────────────────────────────────────────────────────────────────┐
│                     DOUBLE RATCHET OVERVIEW                         │
│                                                                     │
│   ┌─────────────────────┐                                           │
│   │    DH RATCHET       │  Steps on every CONVERSATION TURN change  │
│   │  (Asymmetric)       │  New DH key pair generated                │
│   │                     │  Updates the Root Key                     │
│   │  Provides:          │  Provides POST-COMPROMISE RECOVERY        │
│   │  - New randomness   │  (fresh DH randomness heals from any     │
│   │  - Root Key update  │   prior key compromise)                  │
│   └────────┬────────────┘                                           │
│            │ feeds into                                              │
│   ┌────────▼────────────┐                                           │
│   │  SYMMETRIC RATCHET  │  Steps on every MESSAGE within a turn     │
│   │  (KDF Chain)        │  HMAC-SHA256 chain                        │
│   │                     │  Derives unique Message Key per message   │
│   │  Provides:          │  Provides FORWARD SECRECY per message     │
│   │  - Per-message keys │  (old message keys are deleted;          │
│   │  - Chain progression│   cannot be recomputed)                  │
│   └─────────────────────┘                                           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Ratchet Progression — Detailed Diagram

```
ALICE sends 3 messages, then BOB sends 2 messages, then ALICE sends 1 message:

Root Key (from X3DH)
    │
    │  DH Ratchet Step 1: Alice's turn begins
    │  Alice generates new DH key pair (DH_A1)
    │  RK, CK_A1 = KDF(RK, DH(DH_A1, DH_B0))
    │
    ├──► Chain Key CK_A1
    │       │
    │       ├──► CK_A1.1 ──► Message Key MK_A1 ──► Encrypt Alice Msg 1
    │       ├──► CK_A1.2 ──► Message Key MK_A2 ──► Encrypt Alice Msg 2
    │       └──► CK_A1.3 ──► Message Key MK_A3 ──► Encrypt Alice Msg 3
    │
    │  DH Ratchet Step 2: Bob's turn begins
    │  Bob generates new DH key pair (DH_B1)
    │  RK, CK_B1 = KDF(RK, DH(DH_B1, DH_A1))
    │
    ├──► Chain Key CK_B1
    │       │
    │       ├──► CK_B1.1 ──► Message Key MK_B1 ──► Encrypt Bob Msg 1
    │       └──► CK_B1.2 ──► Message Key MK_B2 ──► Encrypt Bob Msg 2
    │
    │  DH Ratchet Step 3: Alice's turn begins again
    │  Alice generates new DH key pair (DH_A2)
    │  RK, CK_A2 = KDF(RK, DH(DH_A2, DH_B1))
    │
    └──► Chain Key CK_A2
            │
            └──► CK_A2.1 ──► Message Key MK_A4 ──► Encrypt Alice Msg 4
```

### Symmetric Ratchet (KDF Chain)

Within a single sending turn, each message derives its key from the previous chain key:

```
Chain Key (CK_n)
    │
    ├──► HMAC-SHA256(CK_n, 0x01) ──► Message Key (MK_n)    [used to encrypt]
    │
    └──► HMAC-SHA256(CK_n, 0x02) ──► Next Chain Key (CK_n+1) [stored for next msg]

    CK_n is DELETED after deriving MK_n and CK_n+1
    MK_n is DELETED after encrypting/decrypting the message
```

**Forward secrecy per message**: Once MK_n is used and deleted, there is no way to recompute it. Even if CK_n+1 is compromised, CK_n cannot be derived (HMAC is a one-way function).

### DH Ratchet

When the conversation direction changes (Alice was sending, now Bob sends), a new DH ratchet step occurs:

1. Bob generates a **new ephemeral DH key pair** (DH_B_new).
2. Bob computes `DH_output = DH(DH_B_new_private, DH_A_current_public)`.
3. A new Root Key and Chain Key are derived: `RK_new, CK_new = KDF(RK_current, DH_output)`.
4. The old Root Key is deleted.

**Post-compromise recovery**: Even if an attacker compromised a previous chain key or root key, the new DH ratchet step introduces fresh randomness (the new ephemeral key pair). The attacker would need to also compromise the new private key, which is generated independently. After one DH ratchet step, the session is secure again.

### Message Header

Each encrypted message includes a header (sent in the clear):

```
┌──────────────────────────────────────┐
│  Message Header                      │
│  ─────────────────                   │
│  DH public key (current ratchet)     │
│  Previous chain length (N)           │
│  Message number in chain (n)         │
│  ────────────────────────────────    │
│  Encrypted payload (ciphertext)      │
│  HMAC (authentication tag)           │
└──────────────────────────────────────┘
```

The DH public key tells the recipient which ratchet step this message belongs to. The chain length and message number allow handling out-of-order messages.

### Handling Out-of-Order Messages

Messages may arrive out of order (common on mobile networks). The Double Ratchet handles this:
- The recipient stores **skipped message keys** (derived from the chain but not yet used).
- When an out-of-order message arrives, the recipient uses the stored key for that position.
- Skipped keys are stored with a bounded limit and a TTL to prevent memory exhaustion.

---

## 5. Group E2E Encryption — Sender Keys

1:1 encryption uses the Double Ratchet, but groups present a scaling problem. For a group of N members, pairwise encryption means encrypting every message N-1 times. WhatsApp solves this with **Sender Keys**.

### The Scaling Problem

```
PAIRWISE ENCRYPTION FOR GROUPS (naive approach):

  Alice sends a message to a group of N members:

  Alice ──encrypt(msg, key_Alice_Bob)────► Bob
  Alice ──encrypt(msg, key_Alice_Carol)──► Carol
  Alice ──encrypt(msg, key_Alice_Dave)───► Dave
  ...
  Alice ──encrypt(msg, key_Alice_Nth)────► Nth member

  Cost: O(N) encryptions per message
  For 1024-member group: 1023 encryptions per send

  This is prohibitively expensive.
```

### Sender Keys — O(1) Group Encryption

```
SENDER KEY PROTOCOL:

  SETUP PHASE (one-time per sender, per group):
  ┌─────────────────────────────────────────────────────────────┐
  │                                                             │
  │  1. Alice generates a Sender Key (SK_Alice) for this group  │
  │     - Random 32-byte symmetric key                         │
  │     - Plus a Curve25519 signing key pair                   │
  │                                                             │
  │  2. Alice distributes SK_Alice to all group members         │
  │     via their PAIRWISE E2E channels (Double Ratchet)       │
  │                                                             │
  │     Alice ──[pairwise E2E]──► Bob:   SK_Alice               │
  │     Alice ──[pairwise E2E]──► Carol: SK_Alice               │
  │     Alice ──[pairwise E2E]──► Dave:  SK_Alice               │
  │                                                             │
  │  Cost: O(N) pairwise encryptions (but only ONCE per setup) │
  └─────────────────────────────────────────────────────────────┘

  MESSAGE PHASE (every message):
  ┌─────────────────────────────────────────────────────────────┐
  │                                                             │
  │  Alice encrypts message ONCE with her Sender Key:          │
  │                                                             │
  │     ciphertext = AES-256-CBC(msg, SK_Alice_chain_key)      │
  │                                                             │
  │  Server fans out the SAME ciphertext to all members.       │
  │  Each member decrypts with their copy of SK_Alice.         │
  │                                                             │
  │     ┌──────────────┐                                        │
  │     │  Alice sends  │                                        │
  │     │  1 encrypted  │──────► Bob   (decrypt with SK_Alice)  │
  │     │  message      │──────► Carol (decrypt with SK_Alice)  │
  │     │              │──────► Dave  (decrypt with SK_Alice)  │
  │     └──────────────┘                                        │
  │                                                             │
  │  Cost: O(1) encryption per message                         │
  └─────────────────────────────────────────────────────────────┘
```

### Sender Key Ratchet

Each sender's key includes its own **symmetric ratchet** (similar to the chain ratchet in Double Ratchet):

```
Sender Key (SK_Alice) for Group G:
    │
    ├──► HMAC(SK, 0x01) ──► Message Key 1 ──► Encrypt Group Msg 1
    ├──► HMAC(SK, 0x02) ──► Message Key 2 ──► Encrypt Group Msg 2
    └──► ...

    Chain advances with each message Alice sends to the group.
    Recipients advance their copy of Alice's chain in sync.
```

### Key Rotation on Membership Changes

When a member is **removed** from the group:
1. All remaining members generate **new Sender Keys**.
2. New keys are distributed via pairwise channels.
3. The removed member's copy of all Sender Keys is now stale — they cannot decrypt future messages.

When a member is **added** to the group:
1. Existing members generate new Sender Keys and distribute to all members including the new one.
2. The new member generates their own Sender Key and distributes it.

### Trade-offs: Sender Keys vs Pairwise Double Ratchet

| Property | Pairwise Double Ratchet (1:1) | Sender Keys (Groups) |
|---|---|---|
| Encryption cost per message | O(1) — one encryption | O(1) — one encryption |
| Setup cost | O(1) — one X3DH | O(N) — distribute to all members |
| Forward secrecy | Per-message (DH ratchet) | Per-message within sender chain only |
| Post-compromise recovery | Automatic on next DH ratchet step | Requires explicit key rotation |
| Key rotation | Continuous (every turn change) | On membership changes only |
| If sender key compromised | N/A | All future messages from that sender readable until rotation |
| Scalability for groups | O(N) encryptions per message — impractical | O(1) encryption per message — scales to 1024 members |

> **Why the weaker forward secrecy is acceptable**: WhatsApp groups are capped at 1024 members and keys rotate on membership changes. The window of vulnerability (between membership changes) is bounded. For the privacy-critical case (1:1 conversations), the full Double Ratchet with per-message forward secrecy is used.

---

## 6. Server's Role — The Untrusted Relay

The WhatsApp server is designed as an **untrusted relay**. It facilitates message delivery but is cryptographically excluded from reading message content.

### What the Server Stores

| Data | Stored? | Encrypted? | Server Can Read? |
|---|---|---|---|
| Message content (text, media) | Temporarily (until delivered) | E2E encrypted | **No** |
| Pre-key bundles | Yes (public keys only) | N/A (public data) | Yes (but these are public keys) |
| Delivery metadata | Yes | No | **Yes** — who sent to whom, when |
| Group membership | Yes | No | **Yes** — who is in which group |
| Phone number / account info | Yes | No | **Yes** |
| Profile info (name, photo) | Yes | No | **Yes** |
| Last seen / online status | Yes | No | **Yes** |

### What the Server Cannot Do

- **Cannot decrypt messages** — it does not have the session keys.
- **Cannot forge messages** — it does not have the sender's signing keys.
- **Cannot insert itself as a man-in-the-middle** — safety numbers allow users to verify identity keys out-of-band.
- **Cannot silently add participants to a conversation** — group membership changes are visible to members.

### Security Implications of Server Compromise

If an attacker compromises the server:

| Attack | Possible? | Mitigation |
|---|---|---|
| Read past messages | **No** — messages are E2E encrypted and deleted after delivery | Forward secrecy via Double Ratchet |
| Read future messages | **No** — would need to compromise end-device private keys | Keys never transit through server |
| Metadata analysis | **Yes** — who talks to whom, when, how often | This is a known limitation; Signal mitigates with "sealed sender" [WhatsApp does not] |
| Withhold messages (denial of service) | **Yes** — server controls delivery | Users would notice non-delivery |
| Serve malicious pre-key bundles (MitM) | **Theoretically possible** — server could substitute its own keys | Safety number verification prevents this |
| Exhaust one-time pre-keys | **Yes** — force fallback to 3-DH (weaker but still secure) | Clients replenish OPKs proactively |

> **The critical trust assumption**: Users trust that the WhatsApp client software correctly implements the Signal Protocol. The server is untrusted by design, but the **client software** must be trusted. This is why WhatsApp's code is not open-source (unlike Signal), which has drawn criticism from security researchers.

---

## 7. Safety Numbers / Security Codes

Safety numbers provide a mechanism for users to **verify** that their E2E encryption is not being man-in-the-middled by the server.

### How Safety Numbers Work

```
Safety Number = Hash(Alice's Identity Key || Bob's Identity Key)

Represented as:
  - A 60-digit numeric code (displayed in groups of 5)
  - A scannable QR code

Example: 12345 67890 12345 67890 12345 67890
         12345 67890 12345 67890 12345 67890
```

Both Alice and Bob compute the same safety number (the hash is computed over a canonical ordering of the two identity keys). They can compare by:
1. **In person**: One user scans the other's QR code.
2. **Out of band**: Compare the 60-digit number via a phone call or other trusted channel.

### When Safety Numbers Change

The safety number changes when **either party's Identity Key changes**. This happens when:
- The user reinstalls WhatsApp (new Identity Key generated).
- The user switches to a new phone.
- The user adds a linked device [INFERRED — multi-device may use device-level keys].

When the safety number changes, WhatsApp displays a notification:
> "Your security code with [contact] has changed. Tap to learn more."

This could indicate:
- **Benign**: The contact got a new phone or reinstalled the app.
- **Malicious**: A man-in-the-middle attack where the server substituted identity keys.

### Limitation

Safety number verification is **optional and manual**. Most users never verify. This means a sophisticated server-side MitM attack (substituting pre-key bundles) would go undetected by most users. Signal addresses this with features like "sealed sender" and key transparency logs. WhatsApp has announced work on **Key Transparency** (an auditable directory of identity keys) to address this gap.

---

## 8. Multi-Device E2E

WhatsApp's multi-device support (phone + up to 4 companion devices) was one of the hardest engineering challenges, precisely because of E2E encryption.

### The Problem

With E2E encryption, messages are encrypted **to a specific device's keys**. The server cannot decrypt and re-encrypt for other devices. So how do you deliver the same message to multiple devices belonging to the same user?

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     MULTI-DEVICE KEY ARCHITECTURE                │
│                                                                  │
│  Account: Bob                                                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                                                          │   │
│  │  Device 1 (Phone)           Device 2 (Desktop)           │   │
│  │  ┌─────────────────┐       ┌─────────────────┐          │   │
│  │  │ Identity Key 1  │       │ Identity Key 2  │          │   │
│  │  │ Signed Pre-Key 1│       │ Signed Pre-Key 2│          │   │
│  │  │ One-Time PKs    │       │ One-Time PKs    │          │   │
│  │  │ Device Sig      │       │ Device Sig      │          │   │
│  │  └─────────────────┘       └─────────────────┘          │   │
│  │                                                          │   │
│  │  Device 3 (Tablet)          Device 4 (Web)              │   │
│  │  ┌─────────────────┐       ┌─────────────────┐          │   │
│  │  │ Identity Key 3  │       │ Identity Key 4  │          │   │
│  │  │ Signed Pre-Key 3│       │ Signed Pre-Key 4│          │   │
│  │  │ One-Time PKs    │       │ One-Time PKs    │          │   │
│  │  │ Device Sig      │       │ Device Sig      │          │   │
│  │  └─────────────────┘       └─────────────────┘          │   │
│  │                                                          │   │
│  │  Account Signature: signs all Device Identity Keys       │   │
│  │  (proves these devices belong to the same account)       │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Key Concepts

**Each device has its own Identity Key**: Unlike the old WhatsApp model (where the phone was the sole device and relayed messages to WhatsApp Web), each companion device now has its own independent Identity Key pair, pre-key bundles, and Double Ratchet sessions.

**Account Signature + Device Signature**: To bind devices to an account:
- The primary device (phone) signs each companion device's Identity Key.
- This creates a verifiable chain: Account → Device Identity Key.
- When Alice looks up Bob, the server returns pre-key bundles for **all** of Bob's active devices.

**Client-Side Fan-Out**: When Alice sends a message to Bob:
1. Alice fetches pre-key bundles for **all** of Bob's devices.
2. Alice encrypts the message **separately for each device** (separate Double Ratchet session per device).
3. Alice sends N encrypted copies to the server (one per device).
4. The server delivers each copy to the corresponding device.

```
Alice sends to Bob (who has 3 devices):

  Alice ──encrypt(msg, session_Bob_Phone)──────► Bob's Phone
  Alice ──encrypt(msg, session_Bob_Desktop)────► Bob's Desktop
  Alice ──encrypt(msg, session_Bob_Tablet)─────► Bob's Tablet

  3 separate encryptions, 3 separate Double Ratchet sessions.
```

**Cost**: Sending to a user with N devices requires N encryptions. For a group of M members, each with an average of D devices, sending requires M * D encryptions using Sender Keys (the sender key is distributed to each device independently).

### Why Not a Single Account-Level Key?

A single account-level key shared across devices would require that key to exist on all devices simultaneously — creating a single point of compromise. Per-device keys mean compromising one device does not compromise others. Each device independently provides forward secrecy and post-compromise recovery through its own Double Ratchet.

### History Sync

New companion devices do NOT get message history from the server (the server does not have it — messages are E2E encrypted and deleted after delivery). Instead:
- The primary device (phone) encrypts and transfers message history to the new companion device over a local or E2E encrypted channel.
- This is why the phone was originally required to be online for WhatsApp Web to work.
- With the current multi-device architecture, companion devices maintain their own message stores and can operate independently of the phone.

---

## 9. Encrypted Backups

WhatsApp introduced **end-to-end encrypted cloud backups** in 2021, addressing a major gap: previously, backups to iCloud/Google Drive were unencrypted, meaning Apple or Google (or law enforcement with a warrant) could read them.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   ENCRYPTED BACKUP FLOW                          │
│                                                                  │
│   USER'S DEVICE                                                  │
│   ┌──────────────────────────────────────────┐                   │
│   │  1. User chooses backup password          │                   │
│   │     OR generates a 64-digit random key    │                   │
│   │                                           │                   │
│   │  2. Derive encryption key from password   │                   │
│   │     (PBKDF2 / Argon2 or similar KDF)     │                   │
│   │                                           │                   │
│   │  3. Encrypt full chat backup with         │                   │
│   │     AES-256-GCM using derived key        │                   │
│   │                                           │                   │
│   │  4. Upload encrypted backup to            │                   │
│   │     iCloud / Google Drive                 │                   │
│   └──────────────────────────────────────────┘                   │
│                                                                  │
│   HSM-BASED BACKUP KEY VAULT (Meta's infrastructure)             │
│   ┌──────────────────────────────────────────┐                   │
│   │  If password-based:                       │                   │
│   │  - The encryption key is stored in an     │                   │
│   │    HSM (Hardware Security Module) cluster │                   │
│   │  - HSM stores: hash(password) → key      │                   │
│   │  - Rate limited: max attempts before      │                   │
│   │    permanent lockout                      │                   │
│   │  - HSM is designed to be tamper-proof:    │                   │
│   │    even Meta cannot extract stored keys   │                   │
│   │                                           │                   │
│   │  If 64-digit key:                         │                   │
│   │  - No server-side storage needed          │                   │
│   │  - User must remember/store the key       │                   │
│   └──────────────────────────────────────────┘                   │
│                                                                  │
│   CLOUD STORAGE (iCloud / Google Drive)                          │
│   ┌──────────────────────────────────────────┐                   │
│   │  Stores: encrypted backup blob            │                   │
│   │  Apple/Google CANNOT decrypt it           │                   │
│   └──────────────────────────────────────────┘                   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### HSM-Based Backup Key Vault

The key innovation is the **Backup Key Vault** — a distributed cluster of Hardware Security Modules (HSMs):

- **Purpose**: Allows password-based backup recovery without exposing the encryption key to Meta.
- **How it works**: The user's password is used to retrieve the backup encryption key from the HSM. The HSM performs the password verification internally and releases the key only on correct password entry.
- **Brute-force protection**: The HSM enforces strict rate limiting. After a fixed number of failed attempts, the key is permanently locked out. This prevents offline brute-force attacks — the attacker must go through the HSM for every attempt.
- **Tamper resistance**: HSMs are designed to destroy their contents if physically tampered with. Even Meta employees with physical access to the HSM cannot extract stored keys.

### Two Recovery Options

| Option | Password-Based | 64-Digit Key |
|---|---|---|
| User experience | Easy to remember | Must store securely (write down, password manager) |
| Recovery | Enter password → HSM verifies → releases encryption key | Enter 64-digit key directly |
| Risk | Brute-force (mitigated by HSM rate limiting) | Lost key = lost backup (no recovery) |
| Server dependency | HSM cluster must be available | None (fully self-sovereign) |

### Why This Matters

Before encrypted backups, the E2E encryption had a major hole:
- Messages were E2E encrypted **in transit** and **on device**.
- But backups to iCloud/Google Drive were **unencrypted**.
- Law enforcement could subpoena Apple/Google for backup data and read all messages.
- Encrypted backups close this gap — even with a subpoena, the backup is unreadable without the user's password or key.

---

## 10. Architectural Implications — The Cost of Privacy

E2E encryption is not free. It imposes fundamental constraints on the entire system architecture.

### What E2E Encryption Prevents

| Feature | Why It's Impossible | How Competitors Handle It |
|---|---|---|
| **Server-side search** | Server cannot read message content — cannot index it | Telegram: cloud storage, server-side search. Slack: full-text search (messages in plaintext) |
| **Content moderation** | Server cannot inspect messages for spam, abuse, illegal content | Telegram: server-side moderation. Slack: admin content policies |
| **Server-side analytics** | Cannot analyze message content for product insights | Metadata analysis only (who, when, how often — not what) |
| **Seamless multi-device sync** | Each device needs separate E2E sessions; history must transfer device-to-device | Telegram: instant sync from cloud. Slack: all devices read from server |
| **Cloud message history** | Server deletes messages after delivery (transient relay) | Telegram: unlimited cloud history. Slack: searchable archive |
| **Debug message delivery** | Cannot inspect message content to diagnose issues | Can only debug delivery metadata (was message delivered? when?) |
| **Rich link previews (server-side)** | Server cannot read URLs in messages to generate previews | WhatsApp generates previews client-side before sending |
| **Spam detection on content** | Cannot scan message text for spam patterns | Can only use metadata signals (rapid send rate, new account, etc.) |
| **Legal compliance (data retention)** | Cannot retain readable message content per legal requirements | Slack: enterprise compliance with data retention policies |

### The Operational Cost

For the engineering team:
- **Debugging is harder**: Cannot look at message content to reproduce bugs. Must rely on client-side logs (which are also privacy-sensitive).
- **Abuse prevention is harder**: Cannot detect harmful content in messages. Must use metadata-based signals (behavioral patterns, user reports).
- **Multi-device is harder**: Took WhatsApp years longer than Telegram to ship multi-device support, precisely because of E2E constraints.
- **Backup is harder**: Required building a custom HSM-based key vault to enable encrypted cloud backups.

### The Product Trade-off

```
┌──────────────────────────────────────────────────────┐
│           THE FUNDAMENTAL TRADE-OFF                   │
│                                                       │
│   Privacy ◄────────────────────────► Convenience      │
│                                                       │
│   WhatsApp │█████████████░░░░░░░│ Signal              │
│   (E2E default, limited cloud)                        │
│                                                       │
│   Telegram │░░░░░░██████████████│                      │
│   (Cloud sync, no default E2E)                        │
│                                                       │
│   Slack    │░░░░░░░░░░░░████████│                      │
│   (Full server access, enterprise compliance)         │
│                                                       │
│   ◄ More Privacy          More Convenience ►          │
└──────────────────────────────────────────────────────┘
```

WhatsApp chose **privacy as the default**. This decision cascades through every part of the system design. In an interview, recognizing these cascading implications is what separates an L6 answer from an L5 answer.

---

## 11. Contrast with Telegram

### Default Encryption Model

| Aspect | WhatsApp | Telegram |
|---|---|---|
| Default encryption | E2E (Signal Protocol) — all chats | **Client-server encryption** — Telegram can read messages |
| E2E available? | Always on, non-optional | Only in "Secret Chats" (must be explicitly enabled) |
| Protocol | Signal Protocol (Curve25519, AES-256-CBC, HMAC-SHA256) | MTProto 2.0 (custom protocol, AES-256-IGE, SHA-256) |
| Group encryption | Sender Keys (E2E) | No E2E for groups (server-side only) |
| Forward secrecy | Per-message (Double Ratchet) | In Secret Chats only (DH ratchet); none for cloud chats |

### MTProto 2.0

Telegram uses its own custom cryptographic protocol, MTProto 2.0:
- Designed in-house (not a well-audited standard like Signal Protocol).
- Uses AES-256 in IGE mode (Infinite Garble Extension) — an unusual choice that has received criticism from cryptographers.
- For regular (cloud) chats: client-to-server encryption. The server holds encryption keys and can read messages.
- For Secret Chats: E2E encryption with a DH key exchange and ratcheting, but the implementation differs from Signal Protocol.
- MTProto has undergone security audits with mixed results — some vulnerabilities found and patched.

### Why Telegram Made This Choice

By NOT using E2E encryption by default, Telegram gains:
- **Cloud sync**: Messages stored on Telegram's servers, accessible from any device instantly. No complex device-to-device history transfer needed.
- **Server-side search**: Full-text search across all messages, from any device.
- **Large groups and channels**: Groups up to 200K members, channels with unlimited subscribers. Server-side delivery (no client-side Sender Key distribution).
- **Seamless multi-device**: Log in on a new device and see all history immediately.
- **Bots and integrations**: Server can read messages to enable bot interactions.

The trade-off: Telegram users trust Telegram's servers with their message content. If Telegram's servers are breached (or compelled by a government), message content is exposed.

### Interview Insight

In an interview, the important thing is not to say one approach is "better" — it is to recognize the **trade-off** and articulate it clearly:
- WhatsApp optimizes for **privacy**: users trust no one but the endpoints.
- Telegram optimizes for **convenience**: users trust Telegram's infrastructure.
- Both are valid depending on the threat model and user expectations.

---

## 12. Contrast with Slack / Discord

### No E2E Encryption

Neither Slack nor Discord implements E2E encryption. Messages are stored in **plaintext** (or server-side encrypted at rest, which is different from E2E) on their servers.

### Why Not?

**Enterprise Compliance (Slack)**:
- Enterprises require **admin visibility** into messages for compliance (legal holds, eDiscovery, HR investigations).
- Data Loss Prevention (DLP) policies require scanning outgoing messages for sensitive content.
- Audit logs require message content access.
- E2E encryption would make all of these impossible.
- Slack's threat model: protect data from **external attackers**, not from Slack itself or the organization's admins.

**Community Moderation (Discord)**:
- Discord servers are communities (gaming, interest groups) with moderation needs.
- Server moderators and Discord Trust & Safety team need to detect and remove harmful content (harassment, illegal content, spam).
- Content moderation at scale requires server-side access to message content.
- Discord's threat model: protect users from **other users** within the platform.

### Comparison Table

| Feature | WhatsApp | Telegram | Slack | Discord |
|---|---|---|---|---|
| E2E encryption | Default (all chats) | Optional (Secret Chats only) | None | None |
| Server can read messages | No | Yes (except Secret Chats) | Yes | Yes |
| Message retention | Transient (deleted after delivery) | Cloud (permanent) | Permanent (searchable) | Permanent |
| Server-side search | No | Yes | Yes (full-text) | Yes |
| Content moderation | Metadata-only | Server-side | Server-side + admin policies | Server-side + community mods |
| Compliance / eDiscovery | Not possible | Not standard | Built-in (enterprise feature) | Not standard |
| Multi-device sync | Complex (per-device encryption) | Seamless (cloud) | Seamless (server-stored) | Seamless (server-stored) |
| Threat model | Protect from everyone (including server) | Protect from external attackers | Protect from external attackers + internal compliance | Protect users from each other |

### Different Threat Models

```
WhatsApp:  User ←──── E2E ────► User
           Server is adversary. Protect user data FROM the platform.

Telegram:  User ←── TLS ──► Server ←── TLS ──► User
           Server is trusted intermediary. Protect from external attackers.

Slack:     User ←── TLS ──► Server (stores plaintext) ──► Admin (full access)
           Enterprise admins are authorized readers. Protect from outsiders.

Discord:   User ←── TLS ──► Server (stores plaintext) ──► Mods (content access)
           Community moderators need access. Protect from bad actors in community.
```

---

## 13. Interview Rubric — L5 / L6 / L7

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Protocol knowledge** | "WhatsApp uses E2E encryption" — knows it exists but not how | Names Signal Protocol, explains X3DH and Double Ratchet at a high level. Knows the key hierarchy and why each layer exists | Can explain the cryptographic details: why Curve25519, why HMAC-based KDF, why DH ratchet provides post-compromise recovery. Discusses the formal security proofs and assumptions |
| **Group encryption** | "Groups are also encrypted" | Explains Sender Keys, the O(1) vs O(N) trade-off, and why forward secrecy is weaker in groups | Discusses key rotation on membership changes, the trust model for sender key distribution, and compares with MLS (Messaging Layer Security) protocol for large groups |
| **Server trust model** | "Server can't read messages" | Explains what the server CAN see (metadata), what it CAN'T (content). Discusses implications for search, moderation, debugging | Discusses metadata privacy (sealed sender in Signal), key transparency for preventing MitM, and the philosophical difference between "can't read" vs "chooses not to read" |
| **Multi-device** | "It works on desktop too" | Explains per-device keys, client-side fan-out, N encryptions per send. Understands why multi-device was hard for WhatsApp | Discusses the Account Identity model, Device Signatures, the history sync problem, and compares with Telegram's trivial multi-device (because no E2E) |
| **Backups** | "You can backup to iCloud" | Knows about encrypted backups, HSM-based key vault, password vs 64-digit key options | Discusses the HSM threat model, rate limiting against brute force, the gap before encrypted backups (backup was the weakest link), and regulatory implications |
| **Trade-off articulation** | States that WhatsApp has better privacy | Clearly contrasts WhatsApp vs Telegram vs Slack — explains WHY each made different choices based on different product goals | Frames it as a spectrum (privacy vs convenience), discusses the operational cost of E2E at each layer (search, moderation, debugging, backup), and articulates when E2E is the wrong choice (enterprise compliance) |
| **Architectural cascading** | Does not connect encryption to other design decisions | Explains how E2E constrains storage (transient relay), search (impossible), multi-device (hard), backups (need HSM) | Maps the full cascade: E2E → no server search → client-side search only → device storage limits → backup requirement → HSM infrastructure → still no cross-device search → UX compromise. Identifies this as the defining architectural trade-off |

---

## References

- **WhatsApp Security Whitepaper**: https://www.whatsapp.com/security/WhatsApp-Security-Whitepaper.pdf — Official documentation of WhatsApp's E2E encryption implementation.
- **Signal Protocol Specifications**: https://signal.org/docs/ — The X3DH and Double Ratchet specifications authored by Trevor Perrin and Moxie Marlinspike.
  - X3DH: https://signal.org/docs/specifications/x3dh/
  - Double Ratchet: https://signal.org/docs/specifications/doubleratchet/
- **Meta Engineering Blog — Multi-Device**: https://engineering.fb.com/2021/07/14/security/whatsapp-multi-device/ — Details on WhatsApp's multi-device E2E architecture.
- **Meta Engineering Blog — Encrypted Backups**: https://engineering.fb.com/2021/09/10/security/whatsapp-e2ee-backups/ — HSM-based Backup Key Vault design.
- **WhatsApp Key Transparency**: https://engineering.fb.com/2023/04/13/security/whatsapp-key-transparency/ — Auditable key directory to prevent server-side MitM.

---

*This document is part of the WhatsApp System Design series. See also:*
- [01-interview-simulation.md](./01-interview-simulation.md) — Main interview dialogue
- [03-messaging-and-delivery.md](./03-messaging-and-delivery.md) — Message delivery pipeline
- [04-connection-and-presence.md](./04-connection-and-presence.md) — WebSocket and presence
- [06-storage-and-data-model.md](./06-storage-and-data-model.md) — Data storage
- [07-group-messaging.md](./07-group-messaging.md) — Group chat architecture
