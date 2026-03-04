# DynamoDB Transactions — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [TransactWriteItems](#2-transactwriteitems)
3. [TransactGetItems](#3-transactgetitems)
4. [Two-Phase Protocol](#4-two-phase-protocol)
5. [Isolation Levels](#5-isolation-levels)
6. [Conflict Detection and Handling](#6-conflict-detection-and-handling)
7. [Idempotency](#7-idempotency)
8. [Cost and Capacity](#8-cost-and-capacity)
9. [Limitations](#9-limitations)
10. [Interview Angles](#10-interview-angles)

---

## 1. Overview

DynamoDB transactions provide ACID guarantees across up to 100 items, potentially
spanning multiple tables within the same AWS account and region.

| Property | Value |
|----------|-------|
| Max items per transaction | 100 |
| Max aggregate size | 4 MB |
| Scope | Same account, same region |
| Cost | 2x normal (prepare + commit) |
| Idempotency token | ClientRequestToken, valid 10 minutes |
| Isolation (transactional ops) | Serializable |
| Isolation (non-transactional reads) | Read-committed |
| Cross-table | Yes (same region) |
| Cross-region (Global Tables) | No |

---

## 2. TransactWriteItems

### 2.1 Supported Actions

A single TransactWriteItems call can contain up to 100 actions of these types:

| Action | Operation | Description |
|--------|-----------|-------------|
| `Put` | PutItem | Create or replace an item |
| `Update` | UpdateItem | Modify attributes or add new item |
| `Delete` | DeleteItem | Remove an item |
| `ConditionCheck` | — | Verify a condition without modifying the item |

### 2.2 Properties

- **All-or-nothing:** Either all actions succeed or none succeed
- **No partial results:** If any action fails (condition not met, conflict), entire transaction is cancelled
- **Cannot target same item twice:** A single transaction cannot include two actions on the same item (same PK + SK)
- **Cross-table:** Actions can span multiple tables in the same region

### 2.3 Example

```json
{
  "TransactItems": [
    {
      "Put": {
        "TableName": "Orders",
        "Item": {
          "OrderId": {"S": "O001"},
          "CustomerId": {"S": "C001"},
          "Amount": {"N": "50"},
          "Status": {"S": "CREATED"}
        },
        "ConditionExpression": "attribute_not_exists(OrderId)"
      }
    },
    {
      "Update": {
        "TableName": "Inventory",
        "Key": {"ProductId": {"S": "P001"}},
        "UpdateExpression": "SET stock = stock - :qty",
        "ConditionExpression": "stock >= :qty",
        "ExpressionAttributeValues": {":qty": {"N": "1"}}
      }
    },
    {
      "Update": {
        "TableName": "Customers",
        "Key": {"CustomerId": {"S": "C001"}},
        "UpdateExpression": "SET totalOrders = totalOrders + :one",
        "ExpressionAttributeValues": {":one": {"N": "1"}}
      }
    }
  ]
}
```

This atomically:
1. Creates the order (only if it doesn't already exist)
2. Decrements inventory (only if sufficient stock)
3. Increments customer order count

If any condition fails, nothing happens.

### 2.4 ConditionCheck Action

ConditionCheck verifies a condition on an item without modifying it:

```json
{
  "ConditionCheck": {
    "TableName": "Users",
    "Key": {"UserId": {"S": "U001"}},
    "ConditionExpression": "accountStatus = :active",
    "ExpressionAttributeValues": {":active": {"S": "ACTIVE"}}
  }
}
```

Use case: Ensure a precondition holds (e.g., user is active) as part of a transaction
that writes to other tables.

---

## 3. TransactGetItems

### 3.1 Properties

- Reads up to 100 items atomically
- Returns a consistent snapshot — all items reflect the same point in time
- Always reads the latest committed values (similar to strongly consistent)
- Can span multiple tables in the same region

### 3.2 Example

```json
{
  "TransactItems": [
    {
      "Get": {
        "TableName": "Orders",
        "Key": {"OrderId": {"S": "O001"}}
      }
    },
    {
      "Get": {
        "TableName": "Inventory",
        "Key": {"ProductId": {"S": "P001"}}
      }
    },
    {
      "Get": {
        "TableName": "Customers",
        "Key": {"CustomerId": {"S": "C001"}}
      }
    }
  ]
}
```

All three items are read at the same point in time — no other transaction can modify
any of them between the reads.

### 3.3 When to Use TransactGetItems vs GetItem

```
GetItem (SC): Read one item, guaranteed latest
  → Use for single-item reads

TransactGetItems: Read multiple items, guaranteed consistent snapshot
  → Use when you need to see a consistent view across multiple items
  → Example: Read an order + its line items + customer — all at same point in time

BatchGetItem: Read multiple items, no consistency guarantee across items
  → Use for bulk reads where per-item consistency is sufficient
  → Cheaper than TransactGetItems (1x vs 2x RCU)
```

---

## 4. Two-Phase Protocol

### 4.1 How DynamoDB Implements Transactions [INFERRED]

DynamoDB uses a two-phase protocol internally:

```
┌────────────────────────────────────────────────────────────┐
│              Two-Phase Transaction Protocol                 │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  Phase 1: PREPARE                                          │
│  ─────────────────                                         │
│  For each item in the transaction:                         │
│    1. Acquire a lock on the item                           │
│    2. Evaluate condition expression (if any)               │
│    3. If condition fails → abort entire transaction        │
│    4. If another transaction holds the lock → conflict     │
│                                                            │
│  If ALL items locked and conditions pass → proceed         │
│                                                            │
│  Phase 2: COMMIT                                           │
│  ────────────────                                          │
│  For each item in the transaction:                         │
│    1. Apply the write (Put/Update/Delete)                  │
│    2. Release the lock                                     │
│    3. Replicate via Paxos (per-partition)                  │
│                                                            │
│  If commit succeeds → return 200 OK                       │
│  If commit fails → rollback all changes                   │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### 4.2 Why 2x Cost

Each item in a transaction requires two operations:
1. **Prepare:** Read current state + acquire lock + evaluate condition
2. **Commit:** Apply the write + release lock

```
Normal PutItem (1 KB item): 1 WCU
TransactWriteItems PutItem (1 KB item): 2 WCU (prepare + commit)

Normal GetItem (4 KB item, SC): 1 RCU
TransactGetItems Get (4 KB item): 2 RCU (prepare + commit)
```

### 4.3 Transaction Coordinator [INFERRED]

```
Client          Transaction         Partition A       Partition B       Partition C
                Coordinator         (Leader)          (Leader)          (Leader)
  │                  │                  │                 │                 │
  │ TransactWrite    │                  │                 │                 │
  │ [item-A, item-B, │                  │                 │                 │
  │  item-C]         │                  │                 │                 │
  │─────────────────▶│                  │                 │                 │
  │                  │                  │                 │                 │
  │                  │── PREPARE(A) ───▶│                 │                 │
  │                  │── PREPARE(B) ────────────────────▶│                 │
  │                  │── PREPARE(C) ───────────────────────────────────────▶│
  │                  │                  │                 │                 │
  │                  │◀─ PREPARED(A) ──│                 │                 │
  │                  │◀─ PREPARED(B) ───────────────────│                 │
  │                  │◀─ PREPARED(C) ──────────────────────────────────────│
  │                  │                  │                 │                 │
  │                  │  All prepared ✓  │                 │                 │
  │                  │                  │                 │                 │
  │                  │── COMMIT(A) ────▶│                 │                 │
  │                  │── COMMIT(B) ─────────────────────▶│                 │
  │                  │── COMMIT(C) ────────────────────────────────────────▶│
  │                  │                  │                 │                 │
  │                  │◀─ COMMITTED ────│                 │                 │
  │                  │◀─ COMMITTED ────────────────────│                 │
  │                  │◀─ COMMITTED ───────────────────────────────────────│
  │                  │                  │                 │                 │
  │◀── 200 OK ──────│                  │                 │                 │
```

### 4.4 What Happens If Coordinator Fails?

```
Case 1: Coordinator fails BEFORE all prepares complete
  → Items that were prepared: locks time out and release
  → Transaction never committed → no effect
  → Client sees timeout, retries (with idempotency token)

Case 2: Coordinator fails AFTER all prepares but BEFORE all commits
  → Recovery process detects in-progress transaction
  → Either completes the commits or rolls back
  → Idempotency token ensures client retry doesn't double-commit

Case 3: Coordinator fails AFTER all commits
  → Transaction succeeded, client may not know
  → Client retries with same idempotency token → gets success
```

---

## 5. Isolation Levels

### 5.1 Serializable Isolation

**Between transactional operations and single-item operations:**

| Op A | Op B | Isolation |
|------|------|-----------|
| TransactWriteItems | PutItem | Serializable |
| TransactWriteItems | UpdateItem | Serializable |
| TransactWriteItems | DeleteItem | Serializable |
| TransactWriteItems | GetItem | Serializable |
| TransactWriteItems | TransactGetItems | Serializable |
| TransactWriteItems | TransactWriteItems | Serializable |

**What serializable means:** The result is the same as if one operation completed
entirely before the other started. No interleaving.

### 5.2 Read-Committed Isolation

**Between transactional operations and multi-item reads:**

| Op A | Op B | Isolation |
|------|------|-----------|
| TransactWriteItems | Query | Read-committed |
| TransactWriteItems | Scan | Read-committed |
| TransactWriteItems | BatchGetItem | Read-committed |

**What read-committed means:** Multi-item reads never see uncommitted data, but they
may see partial results of a committed transaction:

```
TransactWriteItems modifies items A, B, C atomically

Concurrent Query (reading A, B, C):
  → May see: new A, old B, new C  (partial view)
  → Will NEVER see: uncommitted values
  → This is read-committed, NOT serializable

To get serializable multi-item reads: use TransactGetItems
```

### 5.3 BatchWriteItem Isolation

BatchWriteItem is NOT serializable as a unit:

```
BatchWriteItem([Write-A, Write-B, Write-C]):
  → Each individual write is serializable with transactions
  → But the BATCH as a whole is not atomic
  → A concurrent transaction may see Write-A committed
    but Write-B not yet committed

BatchWriteItem is: individual items serializable, batch not atomic
```

---

## 6. Conflict Detection and Handling

### 6.1 When Conflicts Occur

| Scenario | Exception | Who Fails |
|----------|-----------|-----------|
| PutItem/UpdateItem/DeleteItem conflicts with ongoing TransactWriteItems | `TransactionConflictException` | The non-transactional write |
| TransactWriteItems conflicts with another TransactWriteItems (same item) | `TransactionCanceledException` | One of the transactions |
| TransactGetItems conflicts with ongoing writes | `TransactionCanceledException` | The TransactGetItems |

### 6.2 Conflict Detection Mechanism [INFERRED]

```
During PREPARE phase:
  Coordinator tries to lock each item

  If item is already locked by another transaction:
    → CONFLICT detected
    → Current transaction is cancelled
    → TransactionCanceledException returned

  If item is being modified by a non-transactional write:
    → Non-transactional write gets TransactionConflictException
    → Transaction continues

Lock duration:
  → Locks are held from PREPARE until COMMIT completes
  → Lock timeout prevents deadlocks [INFERRED]
```

### 6.3 TransactionCanceledException Details

```json
{
  "CancellationReasons": [
    {
      "Code": "None",
      "Message": null
    },
    {
      "Code": "ConditionalCheckFailed",
      "Message": "The conditional request failed"
    },
    {
      "Code": "None",
      "Message": null
    }
  ]
}
```

The `CancellationReasons` array is ordered to match the `TransactItems` array.
In this example, the second item's condition failed.

### 6.4 Minimizing Conflicts

1. **Small transactions:** Fewer items = fewer locks = fewer conflicts
2. **Short transactions:** Quick prepare+commit = shorter lock duration
3. **Avoid hot items:** Don't put frequently-updated items in transactions
4. **Partition separation:** Items in different partitions reduce contention
5. **Retry with backoff:** SDK should retry TransactionCanceledException

---

## 7. Idempotency

### 7.1 ClientRequestToken

```json
{
  "TransactItems": [...],
  "ClientRequestToken": "unique-request-id-12345"
}
```

| Property | Value |
|----------|-------|
| Purpose | Ensures idempotent execution |
| Validity | 10 minutes after request completion |
| First call | Executes transaction, returns write capacity consumed |
| Subsequent calls (same token, same params) | Returns success without re-executing, returns read capacity consumed |
| Same token, different params | `IdempotentParameterMismatch` exception |
| After 10 minutes | Token expired, same token treated as new request |

### 7.2 Why Idempotency Matters

```
Scenario without idempotency:
  1. Client sends TransactWriteItems (transfer $100 A→B)
  2. Transaction succeeds
  3. Network timeout — client doesn't receive 200 OK
  4. Client retries the same transaction
  5. $100 transferred TWICE!

With ClientRequestToken:
  1. Client sends TransactWriteItems with token "txn-001"
  2. Transaction succeeds
  3. Network timeout
  4. Client retries with same token "txn-001"
  5. DynamoDB recognizes token → returns success without re-executing
  6. $100 transferred exactly ONCE ✓
```

### 7.3 AWS SDK Behavior

AWS SDKs automatically generate ClientRequestToken if not provided.
This provides built-in idempotency for SDK users.

---

## 8. Cost and Capacity

### 8.1 Cost Formula

```
TransactWriteItems:
  Per item: 2 WCU × ceil(item_size_kb / 1)

  Example: 3 items, each 1.5 KB
    Per item: 2 × ceil(1.5) = 2 × 2 = 4 WCU
    Total: 3 × 4 = 12 WCU

TransactGetItems:
  Per item: 2 RCU × ceil(item_size_kb / 4)

  Example: 3 items, each 2 KB
    Per item: 2 × ceil(2/4) = 2 × 1 = 2 RCU
    Total: 3 × 2 = 6 RCU
```

### 8.2 Comparison with Non-Transactional

| Operation | 1 KB item | 4 KB item | 8 KB item |
|-----------|-----------|-----------|-----------|
| PutItem | 1 WCU | 1 WCU | 8 WCU |
| TransactWriteItems (Put) | **2 WCU** | **2 WCU** | **16 WCU** |
| GetItem (SC) | 1 RCU | 1 RCU | 2 RCU |
| TransactGetItems (Get) | **2 RCU** | **2 RCU** | **4 RCU** |
| GetItem (EC) | 0.5 RCU | 0.5 RCU | 1 RCU |

### 8.3 DAX Interaction

- **TransactWriteItems through DAX:** DAX passes through to DynamoDB, then calls
  TransactGetItems in background to populate cache → consumes additional RCU
- **TransactGetItems through DAX:** Passes through without caching (no DAX benefit)

### 8.4 Capacity Planning

```
If your application does 100 transactions/sec, each with 5 items of 1 KB:

  Write capacity needed:
    100 × 5 × 2 = 1,000 WCU (for transactions alone)

  Compare to non-transactional:
    100 × 5 × 1 = 500 WCU

  Transaction overhead: 2x
```

---

## 9. Limitations

### 9.1 Hard Limits

| Limit | Value |
|-------|-------|
| Max items per transaction | 100 |
| Max aggregate size | 4 MB |
| Same item in single transaction | Not allowed (once per item) |
| Cross-region | Not supported |
| Cross-account | Not supported |
| GSI reads in transaction | Not supported (base table only) |
| Idempotency token validity | 10 minutes |

### 9.2 Global Tables Limitations

- Transactions are **region-local only** in MREC Global Tables
- Transaction writes are replicated to other regions individually (not atomically)
- Other regions may see partial transaction results during replication
- Transactions are **not supported at all** in MRSC Global Tables

### 9.3 Streams Behavior

- Stream records from a single transaction may appear at **different times**
- Stream does NOT guarantee atomicity of transaction records
- A Lambda consumer may see some items from a transaction but not others

### 9.4 Backup Behavior

- PITR backups taken mid-transaction may contain partial transaction results
- This is because backups are point-in-time snapshots of the physical storage,
  and a transaction's prepare+commit may straddle the backup point

---

## 10. Interview Angles

### 10.1 "How do DynamoDB transactions work?"

"DynamoDB transactions use a two-phase protocol internally. In the prepare phase,
the transaction coordinator acquires locks on all items and evaluates condition
expressions. If all conditions pass and no conflicts are detected, the commit phase
applies the writes and releases locks. This provides serializable isolation for up
to 100 items across multiple tables within the same region. The cost is 2x normal
(prepare + commit), and idempotency is ensured via ClientRequestToken."

### 10.2 "What isolation level do DynamoDB transactions provide?"

```
Two levels, depending on the operation:

Serializable:
  - Between TransactWriteItems and single-item ops (PutItem, GetItem, etc.)
  - Between TransactWriteItems and TransactGetItems
  - Between two TransactWriteItems
  → Result is as if operations executed sequentially

Read-committed:
  - Between TransactWriteItems and Query/Scan/BatchGetItem
  → Multi-item reads never see uncommitted data
  → But may see partial committed results of a transaction
  → Use TransactGetItems for serializable multi-item reads
```

### 10.3 "A transaction keeps failing with TransactionCanceledException. How do you debug?"

```
Step 1: Check CancellationReasons in the exception
  → Each item in the transaction gets a reason code
  → "ConditionalCheckFailed" → condition expression didn't match
  → "TransactionConflict" → another operation locked the item

Step 2: Identify the conflicting item
  → CancellationReasons array matches TransactItems array ordering
  → Find the index with non-None code

Step 3: Determine root cause
  → ConditionalCheckFailed: Data changed between when you read it
    and when the transaction ran. Use SC reads before transacting.
  → TransactionConflict: Another transaction or write is touching
    the same item. Check for:
    - Multiple services writing the same item
    - Retry storms creating competing transactions
    - Batch jobs running during peak traffic

Step 4: Fix
  → Reduce transaction scope (fewer items = fewer conflicts)
  → Add backoff to retries
  → Avoid hot items in transactions
  → Check CloudWatch TransactionConflict metric
```

### 10.4 "Can DynamoDB transactions span multiple tables?"

"Yes, within the same AWS account and region. A single TransactWriteItems can include
actions on items in different tables — for example, creating an order in the Orders
table while decrementing inventory in the Products table. This is one of DynamoDB's
key advantages over some other NoSQL databases that don't support cross-collection
transactions."

### 10.5 "Why is there a 100-item limit on transactions?"

```
The two-phase protocol requires:
  1. Locking ALL items before any writes
  2. Cross-partition coordination (items may span many partitions)
  3. All locks held until commit completes

With more items:
  - More partitions to coordinate → higher latency
  - Longer lock duration → more conflicts
  - Higher probability of at least one conflict → more retries
  - Network of lock dependencies → deadlock risk

100 items balances:
  ✓ Enough for most business operations
  ✓ Low enough to keep latency predictable
  ✓ Low enough to minimize conflict probability
```

### 10.6 Design Decision: Why Not Full Distributed Transactions?

```
DynamoDB could theoretically support unlimited transaction sizes
with a full 2PC or 3PC protocol. Why not?

1. Performance: Lock contention grows super-linearly with transaction size.
   At 1,000 items across 100 partitions, the probability of conflict
   with ANY concurrent operation is very high.

2. Latency: Cross-partition coordination adds latency per partition.
   100 items may span 50+ partitions → 50+ network round trips
   for prepare phase alone.

3. Availability: 2PC requires ALL participants to be available.
   More participants → higher probability of at least one being slow/down.

4. Philosophy: DynamoDB is designed for operational workloads with
   predictable latency. Large transactions conflict with this goal.
   Applications should model data to minimize transaction scope.

Trade-off: Bounded transactions (100 items) with predictable performance
vs. unbounded transactions with unpredictable latency and availability.
```

---

## Appendix: Key Numbers

| Property | Value |
|----------|-------|
| Max items per transaction | 100 |
| Max aggregate size | 4 MB |
| WCU cost multiplier | 2x (prepare + commit) |
| RCU cost multiplier | 2x (prepare + commit) |
| Idempotency token validity | 10 minutes |
| Cross-table | Yes (same region, same account) |
| Cross-region | No |
| Isolation (single-item ops) | Serializable |
| Isolation (Query/Scan/BatchGetItem) | Read-committed |
| MREC Global Tables | Region-local only, non-atomic replication |
| MRSC Global Tables | Not supported |
| Same item in one transaction | Not allowed |
