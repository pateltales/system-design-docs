# Distributed Unique ID Generator — System Flows

> This document depicts and explains all the major flows in the distributed unique ID generator system. Each flow includes a sequence diagram, step-by-step breakdown, and discussion of edge cases.

---

## Table of Contents

1. [ID Generation Flow (Happy Path)](#1-id-generation-flow-happy-path)
2. [Clock Skew Detection and Recovery Flow](#2-clock-skew-detection-and-recovery-flow)
3. [Worker Registration Flow](#3-worker-registration-flow)
4. [Sequence Rollover Flow](#4-sequence-rollover-flow)
5. [Worker Failure and Restart Flow](#5-worker-failure-and-restart-flow)
6. [Batch ID Generation Flow](#6-batch-id-generation-flow)
7. [Timestamp Extraction Flow](#7-timestamp-extraction-flow)
8. [Worker ID Reclamation Flow](#8-worker-id-reclamation-flow)
9. [NTP Synchronization Flow](#9-ntp-synchronization-flow)

---

## 1. ID Generation Flow (Happy Path)

### Single ID Generation — In-Process (Primary Path)

```
Calling Thread              SnowflakeGenerator                  System Clock
    │                             │                                  │
    │  1. nextId()                │                                  │
    │────────────────────────────▶│                                  │
    │                             │                                  │
    │                             │  2. Acquire mutex/lock           │
    │                             │     (CAS or synchronized)       │
    │                             │                                  │
    │                             │  3. Read current time            │
    │                             │────────────────────────────────▶│
    │                             │                                  │
    │                             │  4. timestamp_ms = 1706745600042│
    │                             │◀────────────────────────────────│
    │                             │                                  │
    │                             │  5. relative_ts = timestamp_ms  │
    │                             │     - CUSTOM_EPOCH              │
    │                             │     = 1706745600042             │
    │                             │     - 1577836800000             │
    │                             │     = 128908800042              │
    │                             │                                  │
    │                             │  6. Compare with last_timestamp:│
    │                             │     128908800042 > 128908800041 │
    │                             │     → NEW millisecond!          │
    │                             │     → Reset sequence = 0        │
    │                             │                                  │
    │                             │  7. Compose ID:                 │
    │                             │     id = (relative_ts << 22)    │
    │                             │        | (worker_id << 12)      │
    │                             │        | sequence               │
    │                             │                                  │
    │                             │     = (128908800042 << 22)      │
    │                             │     | (42 << 12)                │
    │                             │     | 0                         │
    │                             │                                  │
    │                             │     = 540,485,047,472,300,032   │
    │                             │                                  │
    │                             │  8. Update state:               │
    │                             │     last_timestamp = 128908800042
    │                             │     sequence = 0                │
    │                             │                                  │
    │                             │  9. Release mutex               │
    │                             │                                  │
    │  10. Return ID              │                                  │
    │◀────────────────────────────│                                  │
    │  540485047472300032         │                                  │
```

### Step-by-Step Breakdown

| Step | Component | Action | Latency |
|------|-----------|--------|---------|
| 1 | Caller | Invokes `nextId()` on the generator instance | ~0 (function call) |
| 2 | Generator | Acquires mutex (CAS spin lock or Java `synchronized`) | ~10-50 ns |
| 3-4 | Generator | Reads system clock (`System.currentTimeMillis()` / `clock_gettime`) | ~20-50 ns |
| 5 | Generator | Subtracts custom epoch (integer subtraction) | ~1 ns |
| 6 | Generator | Compares with `last_timestamp`, resets or increments sequence | ~1 ns |
| 7 | Generator | Bit shifts and OR operations to compose 64-bit ID | ~2 ns |
| 8 | Generator | Updates `last_timestamp` and `sequence` in memory | ~1 ns |
| 9 | Generator | Releases mutex | ~10-50 ns |
| 10 | Generator | Returns 64-bit integer to caller | ~0 |

**Total latency: ~50-150 nanoseconds** (in practice, benchmarks show ~0.5-2 microseconds including OS scheduling jitter)

### Bit Composition Detail

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Step 7: Compose 64-bit ID                                                │
│                                                                          │
│ relative_ts = 128908800042                                               │
│ worker_id   = 42                                                         │
│ sequence    = 0                                                          │
│                                                                          │
│ Operation 1: relative_ts << 22                                           │
│   128908800042 in binary (41 bits):                                      │
│   0 1110000 00010010 10001100 10000000 00101010                          │
│                                                                          │
│   Shift left 22 positions:                                               │
│   0 1110000 00010010 10001100 10000000 00101010 0000000000 000000000000  │
│   └──────────── timestamp (41 bits) ──────────┘└worker(10)┘└─seq(12)──┘  │
│                                                                          │
│ Operation 2: worker_id << 12                                             │
│   42 = 0000101010                                                        │
│   Shift left 12: 0000101010 000000000000                                 │
│                   └─worker──┘└───seq────┘                                │
│                                                                          │
│ Operation 3: Bitwise OR all three                                        │
│   (relative_ts << 22) | (worker_id << 12) | sequence                    │
│   = final 64-bit ID                                                      │
│                                                                          │
│ Final ID layout:                                                         │
│ ┌─┬─────────────────────────────────────────┬──────────┬────────────────┐ │
│ │0│        128908800042                      │    42    │      0         │ │
│ │ │     timestamp (41 bits)                  │worker(10)│  sequence(12)  │ │
│ └─┴─────────────────────────────────────────┴──────────┴────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### Same-Millisecond ID Generation

```
Calling Thread              SnowflakeGenerator                  System Clock
    │                             │                                  │
    │  nextId() — call #1         │                                  │
    │────────────────────────────▶│                                  │
    │                             │  timestamp = 128908800042        │
    │                             │  last_timestamp = 128908800041   │
    │                             │  timestamp > last → new ms      │
    │                             │  sequence = 0                    │
    │  ID: ts|worker|0   ◀────────│                                  │
    │                             │                                  │
    │  nextId() — call #2         │                                  │
    │  (same millisecond!)        │                                  │
    │────────────────────────────▶│                                  │
    │                             │  timestamp = 128908800042        │
    │                             │  last_timestamp = 128908800042   │
    │                             │  timestamp == last → same ms     │
    │                             │  sequence = 1                    │
    │  ID: ts|worker|1   ◀────────│                                  │
    │                             │                                  │
    │  nextId() — call #3         │                                  │
    │  (still same ms!)           │                                  │
    │────────────────────────────▶│                                  │
    │                             │  timestamp = 128908800042        │
    │                             │  sequence = 2                    │
    │  ID: ts|worker|2   ◀────────│                                  │
    │                             │                                  │

    IDs generated:
    ┌──────────────────────────────────────────────────────┐
    │ Call │ Timestamp       │ Worker │ Seq │ ID (decimal)  │
    ├──────┼─────────────────┼────────┼─────┼───────────────┤
    │ #1   │ 128908800042    │ 42     │ 0   │ ...300032     │
    │ #2   │ 128908800042    │ 42     │ 1   │ ...300033     │
    │ #3   │ 128908800042    │ 42     │ 2   │ ...300034     │
    └──────┴─────────────────┴────────┴─────┴───────────────┘

    Note: IDs differ only in the sequence field.
    They are MONOTONICALLY INCREASING (each > previous).
```

---

## 2. Clock Skew Detection and Recovery Flow

### Scenario: System Clock Steps Backwards (NTP Correction)

```
SnowflakeGenerator              System Clock                 NTP Daemon
    │                               │                            │
    │  State:                       │                            │
    │  last_timestamp = 100042      │                            │
    │  sequence = 7                 │                            │
    │                               │                            │
    │  nextId() called              │                            │
    │──────────────────────────────▶│                            │
    │                               │                            │
    │  current_time = 100043        │                            │
    │◀──────────────────────────────│                            │
    │                               │                            │
    │  100043 > 100042 → OK ✓       │                            │
    │  Generate ID normally         │                            │
    │                               │                            │
    │                               │       ┌───────────────────┐│
    │                               │       │ NTP discovers     ││
    │                               │       │ clock is 2000ms   ││
    │                               │       │ ahead. Steps      ││
    │                               │       │ backwards!        ││
    │                               │◀──────┤ time -= 2000ms    ││
    │                               │       └───────────────────┘│
    │                               │                            │
    │  State:                       │                            │
    │  last_timestamp = 100043      │                            │
    │                               │                            │
    │  nextId() called              │                            │
    │──────────────────────────────▶│                            │
    │                               │                            │
    │  current_time = 98043 !!!     │  ← Clock went backwards    │
    │◀──────────────────────────────│     by 2000ms!             │
    │                               │                            │
    │  98043 < 100043               │                            │
    │  CLOCK MOVED BACKWARDS!       │                            │
    │                               │                            │


Strategy A: Refuse (Twitter Snowflake)
──────────────────────────────────────
    │                               │
    │  drift = 100043 - 98043       │
    │       = 2000ms                │
    │                               │
    │  THROW ClockMovedBackwardsError
    │  "Clock moved backwards by    │
    │   2000ms. Refusing to         │
    │   generate IDs."              │
    │                               │
    │  Worker is DOWN for ID gen    │
    │  until clock catches up       │
    │  (~2 seconds)                 │
    │                               │
    │  After 2 seconds...           │
    │  nextId() called              │
    │──────────────────────────────▶│
    │  current_time = 100044        │
    │  100044 > 100043 → OK ✓       │
    │  Resume normal operation      │
    │                               │


Strategy B: Wait (Block Until Caught Up)
────────────────────────────────────────
    │                               │
    │  drift = 100043 - 98043       │
    │       = 2000ms                │
    │                               │
    │  if drift < MAX_WAIT (5000ms):│
    │    sleep(2000ms)              │
    │    ────────── 2 seconds ──────│
    │                               │
    │  current_time = 100043        │
    │  100043 >= 100043 → OK ✓      │
    │  Generate ID normally         │
    │                               │
    │  LOG WARNING: "Waited 2000ms  │
    │  for clock to catch up"       │
    │                               │


Strategy C: Logical Clock (Discord-style)
─────────────────────────────────────────
    │                               │
    │  98043 < 100043               │
    │  → Use last_timestamp instead │
    │    of real clock              │
    │                               │
    │  timestamp = 100043           │
    │  (pretend clock didn't go     │
    │   backwards)                  │
    │                               │
    │  sequence = 8 (increment)     │
    │                               │
    │  Generate ID with:            │
    │  timestamp=100043, seq=8      │
    │                               │
    │  No downtime! But IDs may     │
    │  not reflect real wall time   │
    │  during skew period.          │
    │                               │


Strategy D: Hybrid (Recommended)
────────────────────────────────
    │                               │
    │  drift = last_timestamp       │
    │        - current_time         │
    │        = 100043 - 98043       │
    │        = 2000ms               │
    │                               │
    │  if drift <= 5ms:             │
    │    → Strategy C (logical clock│
    │      silently)                │
    │                               │
    │  if 5ms < drift <= 5000ms:    │
    │    → Strategy C (logical clock│
    │      + LOG WARNING)           │
    │                               │
    │  if drift > 5000ms:           │
    │    → Strategy A (refuse +     │
    │      ALERT on-call)           │
    │                               │
    │  Metrics emitted:             │
    │    clock_backwards_count++    │
    │    clock_drift_ms = 2000      │
```

### Clock Drift Impact Analysis

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Clock Drift Impact Matrix                                                │
│                                                                          │
│ Drift Amount │ Impact on IDs           │ Strategy        │ Recovery     │
│──────────────┼─────────────────────────┼─────────────────┼──────────────│
│ < 1ms        │ None — within jitter    │ Logical clock   │ Automatic    │
│ 1-10ms       │ IDs slightly misordered │ Logical clock   │ Automatic    │
│ 10-100ms     │ Noticeable gap in ts    │ Logical clock   │ Auto + warn  │
│ 100ms-5s     │ Significant drift       │ Logical clock   │ Auto + alert │
│ 5s-60s       │ Major ordering issue    │ Refuse          │ Wait for NTP │
│ > 60s        │ System integrity risk   │ Refuse + kill   │ Manual fix   │
└─────────────────────────────────────────────────────────────────────────┘

Why clock going FORWARD is not a problem:
- IDs just have a future timestamp
- Next IDs will have the same or higher timestamp
- No duplicate risk
- Only issue: timestamp extraction gives slightly wrong time

Why clock going BACKWARD is dangerous:
- Same (timestamp, worker_id) pair could generate same sequence numbers
- DUPLICATE IDs possible if sequence resets to 0 at an already-used timestamp
```

---

## 3. Worker Registration Flow

### Initial Registration via ZooKeeper

```
New Worker Process          ZooKeeper Ensemble              Existing Workers
    │                            │                               │
    │  1. Process starts         │                               │
    │     Read config:           │                               │
    │     zk_connect=zk1:2181,   │                               │
    │     zk2:2181,zk3:2181      │                               │
    │                            │                               │
    │  2. Connect to ZK          │                               │
    │────────────────────────────▶│                               │
    │                            │                               │
    │  3. Establish session      │                               │
    │  session_id = 0x8a3f...    │                               │
    │◀────────────────────────────│                               │
    │                            │                               │
    │  4. Check existing         │                               │
    │     worker nodes           │                               │
    │────────────────────────────▶│                               │
    │                            │                               │
    │  5. Current workers:       │                               │
    │  /snowflake/workers/       │                               │
    │    ├── worker-0 (host-a)   │                               │
    │    ├── worker-1 (host-b)   │                               │
    │    ├── worker-5 (host-c)   │                               │
    │    └── worker-41 (host-d)  │                               │
    │                            │                               │
    │  Available IDs: 2,3,4,     │                               │
    │  6,7,...,40,42,...,1023     │                               │
    │◀────────────────────────────│                               │
    │                            │                               │
    │  6. Create ephemeral node  │                               │
    │  /snowflake/workers/       │                               │
    │  worker-42                 │                               │
    │  data: {                   │                               │
    │    hostname: "host-e",     │                               │
    │    pid: 12345,             │                               │
    │    registered_at: ...      │                               │
    │  }                         │                               │
    │────────────────────────────▶│                               │
    │                            │                               │
    │  7. Node created ✓          │                               │
    │  worker_id = 42            │                               │
    │◀────────────────────────────│                               │
    │                            │                               │
    │  8. Initialize generator   │                               │
    │  SnowflakeGenerator(       │                               │
    │    worker_id=42,           │                               │
    │    epoch=1577836800000     │                               │
    │  )                         │                               │
    │                            │                               │
    │  9. Cache worker_id to     │                               │
    │  local disk:               │                               │
    │  /var/lib/snowflake/       │                               │
    │  worker_id = 42            │                               │
    │                            │                               │
    │  10. Start generating IDs  │                               │
    │  Ready! ✓                   │                               │
    │                            │                               │
    │  11. Begin heartbeat       │                               │
    │  (ZK session keepalive)    │                               │
    │  Every 10s: ping ZK        │                               │
    │─────────(periodic)─────────▶│                               │
    │                            │                               │


Timeline:
──────────────────────────────────────────────────────────
0ms          50ms         100ms            200ms
│            │            │                │
▼            ▼            ▼                ▼
Process      ZK           ZK node          Ready to
starts       connected    created          generate IDs
```

### Registration Failure: ZooKeeper Unavailable

```
New Worker Process          ZooKeeper (DOWN)         Local Disk Cache
    │                            ✗                        │
    │  1. Try connect to ZK      ✗                        │
    │──────────────────────────▶ ✗ TIMEOUT (30s)          │
    │                            ✗                        │
    │  2. Connection failed!     ✗                        │
    │                            ✗                        │
    │  3. Check local cache      ✗                        │
    │────────────────────────────────────────────────────▶│
    │                            ✗                        │
    │  4. Cached worker_id = 42  ✗                        │
    │◀────────────────────────────────────────────────────│
    │                            ✗                        │
    │  5. Decision:              ✗                        │
    │                            ✗                        │
    │  Option A: Use cached ID   ✗                        │
    │  ┌─────────────────────────────────────────┐        │
    │  │ RISK: If this host was replaced and     │        │
    │  │ worker-42 was reassigned to another     │        │
    │  │ host, we'd have TWO workers with ID 42  │        │
    │  │ → DUPLICATE IDS!                        │        │
    │  │                                         │        │
    │  │ Mitigation: Only use cached ID if       │        │
    │  │ - Cache age < 24 hours                  │        │
    │  │ - Same hostname as cached               │        │
    │  │ - Alert ops immediately                 │        │
    │  └─────────────────────────────────────────┘        │
    │                            ✗                        │
    │  Option B: Refuse to start ✗                        │
    │  ┌─────────────────────────────────────────┐        │
    │  │ SAFEST: Don't generate IDs without      │        │
    │  │ confirmed unique worker ID              │        │
    │  │                                         │        │
    │  │ Impact: Service can't generate IDs      │        │
    │  │ until ZK recovers                       │        │
    │  └─────────────────────────────────────────┘        │
```

---

## 4. Sequence Rollover Flow

### When 4,096 IDs are Generated in the Same Millisecond

```
Calling Thread              SnowflakeGenerator              System Clock
    │                             │                              │
    │  State:                     │                              │
    │  last_timestamp = 100042    │                              │
    │  sequence = 4094            │                              │
    │                             │                              │
    │  nextId() — call #4095      │                              │
    │────────────────────────────▶│                              │
    │                             │  timestamp = 100042          │
    │                             │  same ms → seq = 4095        │
    │                             │  (this is the LAST valid     │
    │                             │   sequence number)           │
    │  ID: ts|worker|4095  ◀──────│                              │
    │                             │                              │
    │  nextId() — call #4096      │                              │
    │────────────────────────────▶│                              │
    │                             │  timestamp = 100042          │
    │                             │  same ms → seq = (4095+1)    │
    │                             │            & 0xFFF           │
    │                             │            = 0               │
    │                             │                              │
    │                             │  SEQUENCE ROLLED OVER!       │
    │                             │  seq == 0 after increment    │
    │                             │  → we've exhausted this ms   │
    │                             │                              │
    │                             │  Enter busy-wait loop:       │
    │                             │                              │
    │                             │  ┌─── Spin Loop ──────────┐ │
    │                             │  │                         │ │
    │                             │  │  read clock → 100042   │ │
    │                             │  │  100042 <= 100042? YES  │ │
    │                             │  │  → keep spinning        │ │
    │                             │  │                         │ │
    │                             │  │  read clock → 100042   │ │
    │                             │  │  still same ms...       │ │
    │                             │  │                         │ │
    │                             │  │  read clock → 100042   │ │
    │                             │  │  still same ms...       │ │
    │                             │  │                         │ │
    │                             │  │  read clock → 100043   │ │
    │                             │  │  100043 > 100042? YES!  │ │
    │                             │  │  → BREAK!               │ │
    │                             │  │                         │ │
    │                             │  └─────────────────────────┘ │
    │                             │                              │
    │                             │  New ms! timestamp = 100043  │
    │                             │  sequence = 0                │
    │                             │  Generate ID normally        │
    │                             │                              │
    │  ID: ts=100043|worker|0  ◀──│                              │
    │                             │                              │

Wait time analysis:
─────────────────────
Best case:  ~0 ms   (if the 4096th call happens at the very end of the ms)
Worst case: ~1 ms   (if the 4096th call happens at the very start of the ms)
Average:    ~0.5 ms (uniform distribution of call times within the ms)

This means:
- Sequence rollover adds at most 1ms of latency to that single call
- All other calls in the next millisecond proceed normally
- The calling thread is blocked (spin-waiting), but other threads on
  different workers are unaffected
```

### Impact Analysis

```
┌────────────────────────────────────────────────────────────────────┐
│ Sequence Rollover Impact                                            │
│                                                                     │
│ When does this happen?                                              │
│ ─────────────────────                                               │
│ Only when a SINGLE worker generates > 4,096 IDs in ONE millisecond │
│                                                                     │
│ At what QPS does a single worker hit this?                          │
│ 4,096 IDs / 1 ms = 4,096,000 IDs/sec                              │
│                                                                     │
│ For reference:                                                      │
│ - Twitter's entire system: ~6,000 tweets/sec                       │
│ - A single worker at 4M IDs/sec is extreme                         │
│                                                                     │
│ Real-world likelihood:                                              │
│ ┌──────────────────┬──────────────────────────────────────────────┐ │
│ │ Scenario         │ IDs/sec/worker │ Hits rollover?              │ │
│ ├──────────────────┼────────────────┼─────────────────────────────┤ │
│ │ Tweet creation   │ ~100           │ Never                       │ │
│ │ Message sending  │ ~1,000         │ Never                       │ │
│ │ Event logging    │ ~10,000        │ Never                       │ │
│ │ Click tracking   │ ~100,000       │ Very rarely (burst)         │ │
│ │ Metrics ingest   │ ~1,000,000     │ Occasionally                │ │
│ │ Packet tagging   │ ~4,000,000+    │ Regularly → add workers     │ │
│ └──────────────────┴────────────────┴─────────────────────────────┘ │
│                                                                     │
│ Mitigation for high-throughput scenarios:                           │
│ 1. Add more workers (spread load across worker IDs)                │
│ 2. Use 14-bit sequence (16,384/ms) with 8-bit worker (256 workers) │
│ 3. Pre-generate IDs in a buffer during idle periods                │
└────────────────────────────────────────────────────────────────────┘
```

---

## 5. Worker Failure and Restart Flow

### Worker Process Crash and Recovery

```
Phase 1: Worker Crash
─────────────────────

Worker Process (host-e)     ZooKeeper                    ID Consumers
    │                            │                            │
    │  Generating IDs normally   │                            │
    │  worker_id = 42            │                            │
    │  last_id = ts=100500|42|7  │                            │
    │                            │                            │
    ╳ CRASH!                     │                            │
    ╳ (OOM, segfault, host      │                            │
    ╳  failure, etc.)           │                            │
    ╳                            │                            │
    ╳                            │  Session timeout begins    │
    ╳                            │  (30 seconds default)      │
    ╳                            │                            │
    ╳                            │  No heartbeat from         │
    ╳                            │  session 0x8a3f...         │
    ╳                            │                            │
    ╳                            │  T+30s: Session expired!   │
    ╳                            │                            │
    ╳                            │  Ephemeral node deleted:   │
    ╳                            │  /snowflake/workers/       │
    ╳                            │  worker-42                 │
    ╳                            │                            │
    ╳                            │  worker_id=42 is now       │
    ╳                            │  AVAILABLE for reuse       │
    ╳                            │                            │


Phase 2: New Worker Starts on Same Host
────────────────────────────────────────

New Worker (host-e)         ZooKeeper                    ID Consumers
    │                            │                            │
    │  1. Process starts         │                            │
    │                            │                            │
    │  2. Connect to ZK          │                            │
    │────────────────────────────▶│                            │
    │                            │                            │
    │  3. Check available IDs    │                            │
    │────────────────────────────▶│                            │
    │                            │                            │
    │  4. worker-42 is available │                            │
    │  (old session expired)     │                            │
    │◀────────────────────────────│                            │
    │                            │                            │
    │  5. Register as worker-42  │                            │
    │  Create ephemeral node:    │                            │
    │  /snowflake/workers/       │                            │
    │  worker-42                 │                            │
    │────────────────────────────▶│                            │
    │                            │                            │
    │  6. Registration ✓          │                            │
    │◀────────────────────────────│                            │
    │                            │                            │
    │  7. Initialize generator   │                            │
    │  worker_id = 42            │                            │
    │  last_timestamp = -1       │                            │
    │  sequence = 0              │                            │
    │                            │                            │
    │  8. Resume ID generation   │                            │
    │                            │                            │
    │  First ID: ts=100530|42|0  │                            │
    │  (30 seconds after crash)  │                            │
    │                            │                            │

    Safety analysis:
    ┌──────────────────────────────────────────────────────────────────┐
    │ Q: Can the new worker generate duplicate IDs?                    │
    │                                                                  │
    │ A: NO — because:                                                 │
    │ 1. The crash happened at timestamp 100500                       │
    │ 2. The ZK session took 30 seconds to expire                    │
    │ 3. The new worker starts at timestamp 100530                   │
    │ 4. The new worker's first timestamp (100530) is HIGHER than    │
    │    any timestamp the old worker used (max 100500)              │
    │ 5. Even with the same worker_id, timestamp+sequence is unique  │
    │                                                                  │
    │ The 30-second ZK session timeout acts as a SAFETY GAP that      │
    │ ensures timestamps never overlap between old and new workers.   │
    │                                                                  │
    │ Critical requirement: ZK session timeout MUST be longer than    │
    │ possible clock skew. If a new worker's clock is behind by      │
    │ more than 30 seconds, duplicates become possible.              │
    └──────────────────────────────────────────────────────────────────┘
```

### Race Condition: Overlapping Worker Sessions

```
Dangerous Scenario: Old Worker Still Alive When New Worker Registers
─────────────────────────────────────────────────────────────────────

    This can happen during rolling deployments or network partitions.

Old Worker (host-e)         ZooKeeper           New Worker (host-f)
    │                            │                     │
    │  worker_id = 42            │                     │
    │  generating IDs...         │                     │
    │                            │                     │
    │  Network partition!        │                     │
    │  Can't reach ZK            │                     │
    │  (but still running        │                     │
    │   and generating IDs!)     │                     │
    ╳──────────────╳             │                     │
    │              ╳             │                     │
    │              ╳   T+30s:    │                     │
    │              ╳   Session   │                     │
    │              ╳   expired   │                     │
    │              ╳             │                     │
    │              ╳   worker-42 │                     │
    │              ╳   deleted   │                     │
    │              ╳             │                     │
    │              ╳             │  Register as        │
    │              ╳             │  worker-42          │
    │              ╳             │◀────────────────────│
    │              ╳             │                     │
    │              ╳             │  Granted! ✓          │
    │              ╳             │────────────────────▶│
    │              ╳             │                     │
    │  DANGER!     ╳             │  Start generating   │
    │  Two workers with          │  with worker_id=42  │
    │  worker_id=42!             │                     │
    │                            │                     │


    Mitigation strategies:
    ┌────────────────────────────────────────────────────────────────┐
    │ 1. FENCING TOKEN: When registering, receive a fencing token   │
    │    (monotonic counter). Old worker's token is invalidated.    │
    │    Consumers can reject IDs with old fencing tokens.          │
    │                                                                │
    │ 2. GENERATION COUNTER: Include a "generation" in the worker  │
    │    ID (e.g., use 8 bits for worker + 2 bits for generation). │
    │    New registration increments the generation.                │
    │                                                                │
    │ 3. CLOCK CHECK: On ZK reconnection, if the old worker        │
    │    detects its session expired AND another worker-42 exists,  │
    │    it immediately stops generating IDs and re-registers with  │
    │    a new worker_id.                                           │
    │                                                                │
    │ 4. DRAIN PERIOD: During rolling deploy, old worker stops      │
    │    generating 5 seconds before shutdown. New worker waits     │
    │    5 seconds after registration before starting. Gap ensures  │
    │    no overlap.                                                │
    └────────────────────────────────────────────────────────────────┘
```

---

## 6. Batch ID Generation Flow

```
Client Service              Snowflake REST API             SnowflakeGenerator
    │                             │                              │
    │  POST /v1/id/batch          │                              │
    │  { count: 100 }             │                              │
    │────────────────────────────▶│                              │
    │                             │                              │
    │                             │  1. Validate request:        │
    │                             │     count <= 1000? YES ✓      │
    │                             │                              │
    │                             │  2. Generate 100 IDs:        │
    │                             │     Loop 100 times:          │
    │                             │────────────────────────────▶│
    │                             │     id[0] = nextId()         │
    │                             │◀────────────────────────────│
    │                             │────────────────────────────▶│
    │                             │     id[1] = nextId()         │
    │                             │◀────────────────────────────│
    │                             │     ...                      │
    │                             │────────────────────────────▶│
    │                             │     id[99] = nextId()        │
    │                             │◀────────────────────────────│
    │                             │                              │
    │                             │  3. Total generation time:   │
    │                             │     100 × ~1us = ~100us      │
    │                             │                              │
    │                             │  4. All within same ms?      │
    │                             │     100 < 4096 → likely yes  │
    │                             │                              │
    │  5. 200 OK                  │                              │
    │  { ids: [...],              │                              │
    │    count: 100,              │                              │
    │    timestamp_range: {       │                              │
    │      first: 1706745600042,  │                              │
    │      last: 1706745600042    │                              │
    │    }}                       │                              │
    │◀────────────────────────────│                              │

    Batch spanning multiple milliseconds (large batch):
    ──────────────────────────────────────────────────

    POST /v1/id/batch { count: 5000 }

    Millisecond 1: IDs #0 - #4095    (sequence 0-4095)
                   ↓ sequence rollover → wait for next ms
    Millisecond 2: IDs #4096 - #4999 (sequence 0-903)

    Total time: ~1-2ms for 5000 IDs
    Response includes timestamp_range showing the span

    Performance comparison:
    ┌───────────────────────────────────────────────────────┐
    │ Method           │ 100 IDs  │ Network Calls │ Latency │
    ├───────────────────────────────────────────────────────┤
    │ 100 individual   │ 100 IDs  │ 100 RTTs      │ ~100ms  │
    │ REST calls       │          │               │         │
    │                  │          │               │         │
    │ 1 batch call     │ 100 IDs  │ 1 RTT         │ ~1-2ms  │
    │                  │          │               │         │
    │ SDK batch        │ 100 IDs  │ 0 RTTs        │ ~100us  │
    │ (in-process)     │          │               │         │
    └───────────────────────────────────────────────────────┘
```

---

## 7. Timestamp Extraction Flow

### Decomposing a Snowflake ID into Its Components

```
Input ID                    Extraction Logic                    Output
    │                             │                               │
    │  ID = 540485047472300032    │                               │
    │────────────────────────────▶│                               │
    │                             │                               │
    │                             │  1. Convert to binary (64 bits):
    │                             │                               │
    │                             │  0 00000111100001001010001100  │
    │                             │  10000000001010100000000000    │
    │                             │  00000000000000111             │
    │                             │                               │
    │                             │  2. Extract timestamp (bits 63-22):
    │                             │                               │
    │                             │  timestamp_relative           │
    │                             │    = id >> 22                 │
    │                             │    = 540485047472300032 >> 22 │
    │                             │    = 128908800042             │
    │                             │                               │
    │                             │  timestamp_absolute           │
    │                             │    = 128908800042             │
    │                             │    + 1577836800000 (epoch)    │
    │                             │    = 1706745600042            │
    │                             │                               │
    │                             │  → 2024-02-01T00:00:00.042Z  │
    │                             │                               │
    │                             │  3. Extract worker ID (bits 21-12):
    │                             │                               │
    │                             │  worker_id                    │
    │                             │    = (id >> 12) & 0x3FF       │
    │                             │    = (... >> 12) & 1023       │
    │                             │    = 42                       │
    │                             │                               │
    │                             │  4. Extract sequence (bits 11-0):
    │                             │                               │
    │                             │  sequence                     │
    │                             │    = id & 0xFFF               │
    │                             │    = id & 4095                │
    │                             │    = 0                        │
    │                             │                               │
    │                             │  5. Optional: split worker ID │
    │                             │     into datacenter + machine:│
    │                             │                               │
    │                             │  dc_id = (42 >> 5) & 0x1F    │
    │                             │        = 1                    │
    │                             │  machine_id = 42 & 0x1F      │
    │                             │        = 10                   │
    │                             │                               │
    │                             │                               │
    │     ┌───────────────────────────────────────┐               │
    │     │ Decoded ID:                           │               │
    │     │ ┌──────────────┬──────────────────┐  │               │
    │     │ │ Timestamp    │ 2024-02-01       │  │               │
    │     │ │              │ 00:00:00.042 UTC │  │               │
    │     │ ├──────────────┼──────────────────┤  │               │
    │     │ │ Worker ID    │ 42               │  │               │
    │     │ ├──────────────┼──────────────────┤  │               │
    │     │ │ Datacenter   │ 1                │  │               │
    │     │ ├──────────────┼──────────────────┤  │               │
    │     │ │ Machine      │ 10               │  │               │
    │     │ ├──────────────┼──────────────────┤  │               │
    │     │ │ Sequence     │ 0                │  │               │
    │     │ ├──────────────┼──────────────────┤  │               │
    │     │ │ Age          │ 86400 seconds    │  │               │
    │     │ └──────────────┴──────────────────┘  │               │
    │     └───────────────────────────────────────┘               │
    │◀────────────────────────────────────────────────────────────│


Use case: Querying by time range using IDs
──────────────────────────────────────────

    "Give me all tweets from the last hour"

    Instead of: SELECT * FROM tweets WHERE created_at > NOW() - INTERVAL 1 HOUR
    (requires index on created_at)

    Can do:     SELECT * FROM tweets WHERE id > {snowflake_id_for_1_hour_ago}
    (uses PRIMARY KEY index — much faster!)

    To generate the boundary ID:
    lower_bound_id = ((current_time_ms - epoch - 3600000) << 22)
    //                                          ^^^^^^^^
    //                                          1 hour in ms

    This works because Snowflake IDs are timestamp-ordered!
```

---

## 8. Worker ID Reclamation Flow

### Reclaiming Stale Worker IDs

```
Admin / Monitoring          ZooKeeper                    Stale Worker
    │                            │                            ╳ (dead)
    │                            │                            ╳
    │  1. Detect stale workers   │                            ╳
    │                            │                            ╳
    │  GET /snowflake/workers/   │                            ╳
    │────────────────────────────▶│                            ╳
    │                            │                            ╳
    │  Workers:                  │                            ╳
    │  worker-42:                │                            ╳
    │    session: EXPIRED        │                            ╳
    │    last_heartbeat: 5m ago  │                            ╳
    │                            │                            ╳
    │  ZK auto-cleanup:          │                            ╳
    │  Ephemeral node already    │                            ╳
    │  deleted when session      │                            ╳
    │  expired                   │                            ╳
    │◀────────────────────────────│                            ╳
    │                            │                            ╳
    │  2. worker-42 already      │                            ╳
    │     reclaimed ✓             │                            ╳
    │                            │                            ╳
    │  For NON-ephemeral         │                            ╳
    │  registries (database):    │                            ╳
    │                            │                            ╳
    │  3. Scan worker_registry   │                            ╳
    │     for stale entries:     │                            ╳
    │                            │                            ╳
    │  SELECT worker_id          │                            ╳
    │  FROM worker_registry      │                            ╳
    │  WHERE last_heartbeat      │                            ╳
    │    < NOW() - INTERVAL      │                            ╳
    │      5 MINUTES;            │                            ╳
    │                            │                            ╳
    │  4. Mark as reclaimable:   │                            ╳
    │  UPDATE worker_registry    │                            ╳
    │  SET status = 'RECLAIMED'  │                            ╳
    │  WHERE worker_id = 42;     │                            ╳
    │                            │                            ╳
    │  5. New worker can now     │                            ╳
    │     claim worker_id=42     │                            ╳
```

---

## 9. NTP Synchronization Flow

### How NTP Keeps Clocks Aligned for Snowflake

```
Snowflake Worker            NTP Client (ntpd)           NTP Server (stratum 1)
    │                            │                            │
    │  System clock:             │                            │
    │  1706745600042             │                            │
    │                            │                            │
    │                            │  Poll every 64-1024 seconds│
    │                            │                            │
    │                            │  NTP request (T1)          │
    │                            │────────────────────────────▶│
    │                            │                            │
    │                            │  NTP response (T2,T3,T4)   │
    │                            │◀────────────────────────────│
    │                            │                            │
    │                            │  Calculate offset:         │
    │                            │  offset = ((T2-T1)+(T3-T4))│
    │                            │           / 2              │
    │                            │  = +3.7ms                  │
    │                            │  (our clock is 3.7ms slow) │
    │                            │                            │
    │                            │  Correction strategy:      │
    │                            │                            │
    │  ┌─────────────────────────────────────────────────────┐│
    │  │ Case 1: Small offset (< 128ms) → SLEW               ││
    │  │                                                      ││
    │  │ Gradually adjust clock rate                          ││
    │  │ Speed up by 500 ppm (0.05%)                         ││
    │  │ Takes ~7.4 seconds to correct 3.7ms                 ││
    │  │                                                      ││
    │  │ Clock before: 1706745600042                         ││
    │  │ Clock during: 1706745600043.0005 (slightly faster)  ││
    │  │ Clock after:  1706745603780 (corrected)             ││
    │  │                                                      ││
    │  │ SAFE for Snowflake: clock never goes backwards      ││
    │  └──────────────────────────────────────────────────────┘│
    │                            │                            │
    │  ┌──────────────────────────────────────────────────────┐│
    │  │ Case 2: Large offset (> 128ms) → STEP               ││
    │  │                                                      ││
    │  │ Instant clock jump                                   ││
    │  │                                                      ││
    │  │ If clock is SLOW (offset > 0):                      ││
    │  │   Clock jumps FORWARD → SAFE (higher timestamp)     ││
    │  │                                                      ││
    │  │ If clock is FAST (offset < 0):                      ││
    │  │   Clock jumps BACKWARD → DANGEROUS for Snowflake!   ││
    │  │   This triggers clock skew handling (Flow #2)       ││
    │  └──────────────────────────────────────────────────────┘│
    │                            │                            │


    NTP Best Practices for Snowflake:
    ──────────────────────────────────

    1. Use ntpd with -x flag (slew-only mode)
       → Clock never jumps backwards
       → Trade-off: large offsets take longer to correct

    2. Use multiple NTP sources (at least 3)
       → NTP can detect and ignore a falseticker

    3. Monitor NTP offset as a system metric
       → Alert if |offset| > 10ms
       → Page if |offset| > 100ms

    4. In AWS: use Amazon Time Sync Service
       → 169.254.169.123 (link-local, low latency)
       → Leap second smearing built in
       → Sub-millisecond accuracy

    5. For critical systems: use PTP (Precision Time Protocol)
       → Hardware timestamping
       → Sub-microsecond accuracy
       → Used by financial trading systems
```

---

## Flow Summary

| # | Flow | Trigger | Sync/Async | Hot Path? | Latency |
|---|------|---------|------------|-----------|---------|
| 1 | ID Generation | Application call | Sync | Yes | ~0.5-2 us |
| 2 | Clock Skew Recovery | Clock goes backwards | Sync (blocks caller) | Yes (when triggered) | 0-5000 ms |
| 3 | Worker Registration | Process startup | Sync (one-time) | No | ~50-200 ms |
| 4 | Sequence Rollover | 4096+ IDs in 1ms | Sync (busy-wait) | Yes (when triggered) | 0-1 ms |
| 5 | Worker Failure/Restart | Process crash | Async (ZK timeout) | No | 30s (session timeout) |
| 6 | Batch Generation | Batch API call | Sync | Yes | ~100us per 100 IDs |
| 7 | Timestamp Extraction | Decode API call | Sync | No | ~1 ns (bit shift) |
| 8 | Worker ID Reclamation | Stale worker cleanup | Async (monitoring) | No | Minutes |
| 9 | NTP Synchronization | Periodic (64-1024s) | Async (background) | No | Continuous |

---

*This flow document complements the [interview simulation](interview-simulation.md), [API contracts](api-contracts.md), and [naive-to-scale evolution](naive-to-scale-evolution.md).*
