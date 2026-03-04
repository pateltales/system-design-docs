# AWS Services Technical Deep Dive

> L6/Senior Engineer Interview Preparation - Comprehensive Technical Reference

**Interview Focus**: Limitations, edge cases, performance characteristics, cost implications, and real-world trade-offs

---

## Table of Contents
1. [DynamoDB](#dynamodb)
2. [S3](#s3)
3. [Kinesis](#kinesis)
4. [SQS](#sqs)
5. [SNS](#sns)
6. [EC2](#ec2)
7. [Fargate](#fargate)
8. [Redshift](#redshift)
9. [Glue](#glue)

---

## DynamoDB

### Core Concepts
- **NoSQL database**: Key-value and document store
- **Fully managed**: Auto-scaling, replication, backups
- **Single-digit millisecond latency** at any scale (p50 < 10ms, p99 typically 10-20ms)
- **Eventually consistent** by default (typically < 1 second lag), **strongly consistent** reads available
- **Multi-AZ replication**: Synchronous replication across 3 AZs (within region)

### Table Structure
```
Table
├── Partition Key (PK) — REQUIRED, determines data distribution via MD5 hash
└── Sort Key (SK) — OPTIONAL, enables range queries within partition
```

### Critical Size Limitations

#### Item Size Limit: 400KB (Hard Limit)
**What counts toward the limit:**
- Attribute names (UTF-8 byte length)
- Attribute values (binary, number converted to string, UTF-8 for strings)
- ALL attributes in the item (including PK, SK, and all nested attributes)

**Example calculation:**
```python
# Item:
{
  "userId": "user123",           # Name: 6 bytes, Value: 7 bytes = 13 bytes
  "email": "user@example.com",   # Name: 5 bytes, Value: 16 bytes = 21 bytes
  "profile": {                   # Name: 7 bytes
    "bio": "...",                # Name: 3 bytes, Value: ? bytes
    "photo": "<base64 data>"     # Name: 5 bytes, Value: ? bytes
  }
}
# Total must be < 400KB
```

**What happens at 400KB:**
- Write fails with `ItemSizeTooBig` exception
- Cannot store the item, must split or use S3 pointer pattern

**Interview Question**: *"User wants to store 2MB profile images in DynamoDB"*
```
❌ Bad: Store base64-encoded image (exceeds 400KB)
✅ Good: Store S3 key in DynamoDB, actual image in S3
```

#### Query/Scan Result Size: 1MB (Hard Limit)
**This is the critical limitation you mentioned:**

- **Query** or **Scan** can return **maximum 1MB** of data before pagination
- Pagination happens **BEFORE** FilterExpression is applied
- Must use `LastEvaluatedKey` to continue scanning

**Example scenario (common interview trap):**
```python
# Table has 1000 items, each 5KB
# Query for userId = "user123" (has 300 items = 1.5MB total)

response = query(KeyConditionExpression='userId = :uid')

# Result:
# - Returns ~200 items (1MB worth)
# - response['LastEvaluatedKey'] exists (more data available)
# - Must paginate to get remaining 100 items
```

**Scan + Filter trap (Critical Interview Detail):**
```python
# Scenario: 10GB table, only 10 items match filter (50KB total)
# Question: How many RCUs consumed? Answer: 10GB worth!

# EXECUTION ORDER (this is critical to understand):
# 1. DDB reads UP TO 1MB of raw data from table
# 2. THEN applies FilterExpression to that 1MB
# 3. Returns matching items (could be 0 items!)
# 4. Returns LastEvaluatedKey if more data to scan
# 5. Client paginates, repeats from step 1

response = scan(
    FilterExpression='status = :active'  # Applied AFTER reading data
)

# What actually happens (step-by-step):

# Iteration 1:
# - DDB reads 1MB of data (250 items × 4KB each)
# - Applies filter: 0 items match (all status = 'inactive')
# - Returns: Items=[], LastEvaluatedKey=<token>, ConsumedCapacity=256 RCUs
# - Your app sees: Empty result, but must continue!

# Iteration 2:
# - DDB reads next 1MB (250 items)
# - Applies filter: 1 item matches
# - Returns: Items=[{...}], LastEvaluatedKey=<token>, ConsumedCapacity=256 RCUs
# - Your app sees: 1 item returned

# ... continues for all 10GB ...

# Iteration 10,000:
# - DDB reads last 1MB
# - Applies filter: 2 items match
# - Returns: Items=[{...}, {...}], LastEvaluatedKey=None, ConsumedCapacity=256 RCUs

# TOTAL RESULT:
# - Items returned: 10 items (50KB total)
# - RCUs consumed: 10GB / 4KB = 2,621,440 RCUs
# - Cost: ~$340 (provisioned capacity) or $655 (on-demand)
# - Time: Minutes to hours (depending on RCU capacity)

# WHY SO EXPENSIVE?
# FilterExpression is applied AFTER DDB reads the data
# DDB must read entire 10GB table to find the 10 matching items
# You pay for ALL data scanned (10GB), not just returned data (50KB)

# ✅ Better approaches:
# 1. GSI on 'status' attribute (query directly, costs ~50KB RCUs instead of 10GB)
# 2. Separate table for active items
# 3. Use Query with proper key design (not Scan)
# 4. If Scan unavoidable: Parallel scans across segments (faster, same cost)
```

**Best practice:**
- Paginate using `LastEvaluatedKey` in a loop
- Design schema to avoid large result sets (use sort key ranges)
- Consider GSI with sparse index (only items matching condition)

### Indexes

#### Primary Key Types
1. **Simple Primary Key**: Only Partition Key
   - Each item must have unique PK
   - No range queries possible
   - All items with same PK → ERROR (duplicate key)

2. **Composite Primary Key**: Partition Key + Sort Key
   - Items with same PK must have **different SK**
   - Enables range queries, sorting within partition
   - Uniqueness: PK + SK combination must be unique

#### Global Secondary Index (GSI)
- **Independent index** with its own PK and optional SK
- Can have **different keys** than base table
- **Eventually consistent** only (no strong consistency available)
- Has its own **RCU/WCU** capacity (separate billing)
- **Creation**: Can be created at table creation or added later
- **Limit**: 20 GSIs per table (soft limit, can request increase to 500)
- **Projection**: Choose which attributes to copy
  - `KEYS_ONLY`: Only index keys + base table keys (smallest, cheapest)
  - `INCLUDE`: Keys + specified attributes (middle ground)
  - `ALL`: All attributes (duplicates all data → expensive, doubles storage)

**GSI Critical Limitations & Edge Cases**:

1. **GSI Throttling Blocks Base Table Writes** (Major Gotcha):

**The Critical Problem:**
- When you write to base table, it ALSO writes to ALL GSIs (if item has GSI keys)
- Write must succeed on **BOTH** base table AND GSI
- If GSI throttles → **entire write fails** (even if base table has capacity)

```python
# Scenario: Base table = 1000 WCU, GSI = 100 WCU

# Your app writes at 500 writes/sec
# Each write to base table:
# - Consumes 1 WCU from base table (500/1000 = 50% used ✅)
# - ALSO consumes 1 WCU from GSI (500 needed, only 100 available ❌)

# What happens:
# - First 100 writes/sec: SUCCESS
# - Writes 101-500/sec: FAIL (GSI throttled at 100 WCU limit)
# - Error: ProvisionedThroughputExceededException
# - Base table has 900 WCU idle, but CANNOT use it!

# Why this is confusing:
# - CloudWatch shows base table only 50% utilized
# - You think: "I have plenty of capacity, why throttling?"
# - Answer: GSI is the bottleneck (hidden in metrics)

# Visualized:
Write Request (500/sec)
        ↓
   ┌────┴────┐
   ↓         ↓
Base Table  GSI
1000 WCU    100 WCU ← BOTTLENECK!
 50% used   100% used (throttling)
   ↓         ↓
   ✅        ❌ → Entire write FAILS
```

**Solution: GSI WCU ≥ Base Table WCU**

This means: **Provision GSI write capacity ≥ base table write capacity**

```python
# ✅ Solution 1: Match capacities
Base Table WCU: 1000
GSI WCU:        1000  # Equal to base table

# Now 500 writes/sec:
# - Base: 500/1000 ✅
# - GSI: 500/1000 ✅
# No throttling!

# ✅ Solution 2: On-Demand mode (simplest)
aws dynamodb update-table \
  --table-name MyTable \
  --billing-mode PAY_PER_REQUEST

# On-demand auto-scales BOTH base table AND all GSIs together
# No capacity planning, no GSI throttling risk
# Tradeoff: ~5-7× more expensive per request

# ✅ Solution 3: Auto-scaling (provision both)
# MUST configure auto-scaling for base table AND each GSI
# Common mistake: only scale base table, forget GSI

# ❌ Wrong:
Base Table: Auto-scaling (10-1000 WCU)
GSI:        Fixed at 100 WCU  ← Will throttle when base scales!

# ✅ Right:
Base Table: Auto-scaling (10-1000 WCU)
GSI:        Auto-scaling (10-1000 WCU)  ← Scales with base
```

**How to Detect This Problem:**

```python
# CloudWatch metrics:
# 1. Table: ConsumedWriteCapacityUnits = 500
# 2. Table: ProvisionedWriteCapacityUnits = 1000
# 3. Table: UserErrors > 0 (throttling)
# 4. GSI: WriteThrottleEvents > 0 ← This is the culprit!

# Diagnosis:
# If UserErrors > 0 AND WriteThrottleEvents(GSI) > 0
# → GSI is throttling, blocking base table writes

# Fix immediately:
aws dynamodb update-table \
  --table-name MyTable \
  --global-secondary-index-updates '[{
    "Update": {
      "IndexName": "MyGSI",
      "ProvisionedThroughput": {
        "WriteCapacityUnits": 1000  # Match base table
      }
    }
  }]'
```

**Real-World Interview Scenario:**

*"Your DynamoDB table has 5000 WCU. CloudWatch shows only 2000 WCU consumed (40% utilization), but you're getting throttling errors. What's wrong?"*

**Answer:** "Most likely a GSI bottleneck. When a table has GSIs with independent WCU settings, writes must succeed on both the base table and all applicable GSIs. If a GSI is provisioned with lower WCU than the base table (say 1000 WCU), it will throttle at 1000 writes/sec even though the base table can handle 5000. The write fails on the GSI, which causes the entire base table write to fail. CloudWatch shows base table only 40% utilized because the GSI is blocking writes before they fully consume base capacity. Solution: ensure GSI WCU ≥ base table WCU, or use on-demand mode where both auto-scale together."

**Cost Impact Example:**

```python
# Bad configuration:
Base Table: 5000 WCU × $0.00065/hour × 730 hours = $2,372/month
GSI (email): 100 WCU × $0.00065/hour × 730 hours = $47/month
Total: $2,419/month

# But: Can only write 100 items/sec (GSI limit)
# Effective cost: $2,419 for 100 writes/sec = $24.19 per write/sec

# Good configuration:
Base Table: 5000 WCU × $0.00065/hour × 730 hours = $2,372/month
GSI (email): 5000 WCU × $0.00065/hour × 730 hours = $2,372/month
Total: $4,744/month

# Can write 5000 items/sec (full utilization)
# Effective cost: $4,744 for 5000 writes/sec = $0.95 per write/sec

# 96% better value (and no throttling)!
```

2. **Eventual Consistency Lag** (typically ms, can spike to seconds):
```python
# Write item to base table
put_item(Item={'userId': '123', 'status': 'active'})

# Immediately query GSI by status
result = query(IndexName='status-index',
               KeyConditionExpression='status = :active')

# May NOT see the item yet (eventual consistency)
# Typical lag: < 100ms, but can be seconds during high load
```

3. **Sparse Index Behavior**:
```python
# Base table: 1M items
# GSI on 'premiumTier' attribute
# Only 10K items have 'premiumTier' attribute

# GSI contains: 10K items (not 1M)
# Storage cost: 10K items (huge savings)
# Pattern: Add attribute only when needed (e.g., premium users)
```

4. **Projection Attribute Limitations**:
- **Max projected attributes**: No hard limit, but item size still 400KB
- **Cannot change projection** after creation (must delete/recreate GSI)
- **Projection overhead**: Every write to base table → write to ALL GSIs with that attribute

5. **Online Index Creation** (adding GSI to existing table):
```
# Table has 10TB data
# Add new GSI → backfills existing data

# Behavior:
# - Backfill can take hours/days (depends on table size)
# - Base table remains ACTIVE (can still read/write)
# - GSI status: CREATING → BACKFILLING → ACTIVE
# - No additional cost for backfill (uses table's WCU)
# - During backfill: queries to GSI fail

# Monitoring: Check GSI backfill progress
# - DescribeTable → GSI → Backfilling: true/false
```

6. **GSI Storage = Projected Attributes Only**:
```python
# Base table item: 100KB (50 attributes)
# GSI projects: KEYS_ONLY (userId, status, base PK/SK)

# Storage cost:
# - Base table: 100KB per item
# - GSI: ~1KB per item (only 4 attributes)
# Result: GSI is 100× cheaper to store
```

**Interview Question**: *"GSI is throttling but base table has plenty of capacity. Why?"*
```
Answer: GSI has independent RCU/WCU. Even if base table has capacity,
writes fail if GSI is throttled because writes must succeed on BOTH.

Solution:
1. Set GSI WCU ≥ base table WCU
2. Use on-demand mode (auto-scales both)
3. Monitor GSI throttling metrics separately
```

#### Local Secondary Index (LSI)
- **Same partition key** as base table, **different sort key**
- Must be created **at table creation time** (cannot add later - ever)
- Shares RCU/WCU with base table (no separate provisioning)
- Supports **strong consistency** (unlike GSI)
- **Limit**: 5 LSIs per table (hard limit, cannot increase)
- All LSI data stored in same partition as base table

**LSI Critical Limitations**:

1. **10GB Partition Size Limit** (with LSI):
```python
# Without LSI: Partition can grow indefinitely (DDB auto-splits)
# With LSI: Partition limited to 10GB total

# 10GB includes:
# - Base table items for that partition key
# - ALL LSI items for that partition key

# Example:
# userId = "user123" (partition key)
# - Base table items for user123: 8GB
# - LSI items for user123: 3GB
# Total: 11GB → ERROR: ItemCollectionSizeLimitExceededException

# This is a HARD LIMIT - writes fail when exceeded
```

**Interview Trap**: *"Can I add LSI to existing table?"*
```
❌ No - LSI must be created at table creation time
❌ Cannot add LSI after table exists (even if empty)
✅ Must delete table, recreate with LSI, reload data
✅ Or migrate to new table with LSI (blue/green deployment)
```

2. **LSI vs GSI Decision Matrix**:

| Feature | LSI | GSI |
|---------|-----|-----|
| **When to create** | Table creation only | Anytime |
| **Partition key** | Same as base table | Different allowed |
| **Sort key** | Different from base | Different allowed |
| **Consistency** | Strong + eventual | Eventual only |
| **Capacity** | Shares with base | Independent |
| **Throttling** | Can't throttle (shares) | Can throttle base writes |
| **Partition limit** | 10GB per PK value | No limit |
| **Max count** | 5 (hard) | 20 (soft, can increase) |
| **Sparse** | No (all items included) | Yes (only if attrs exist) |

3. **Strong Consistency on LSI**:
```python
# Query LSI with strong consistency (only possible with LSI)
response = query(
    IndexName='timestamp-index',  # LSI
    KeyConditionExpression='userId = :uid AND timestamp > :time',
    ConsistentRead=True  # ✅ Allowed for LSI, ❌ ERROR for GSI
)

# RCU cost: Same as base table (1 RCU = 4KB strongly consistent)
```

**When to use LSI**:
- Need strong consistency on alternate sort key
- Query patterns known upfront (can't add later)
- Partition data < 10GB
- Example: User messages table, query by userId + timestamp (LSI) or userId + priority (another LSI)

**When to use GSI**:
- Different partition key needed
- Might add index later
- Partition data > 10GB
- Eventually consistent is acceptable
- Example: Query users by email (GSI), by subscription tier (GSI)

### DynamoDB Hard Limits (Critical for Interviews)

| Limit | Value | Type | What Happens When Exceeded |
|-------|-------|------|----------------------------|
| **Item size** | 400KB | Hard | `ItemSizeTooBig` exception |
| **Query/Scan result** | 1MB per call | Hard | Auto-pagination via `LastEvaluatedKey` |
| **Partition key value** | 2048 bytes | Hard | Write fails |
| **Sort key value** | 1024 bytes | Hard | Write fails |
| **Attribute name** | 64KB | Hard | Write fails |
| **LSI per table** | 5 | Hard | Cannot create more |
| **GSI per table** | 20 (soft), 500 (hard) | Soft | Request limit increase |
| **LSI partition size** | 10GB | Hard | `ItemCollectionSizeLimitExceededException` |
| **Batch operations** | 25 items or 16MB | Hard | Split into multiple batches |
| **Transaction items** | 100 items or 4MB | Hard | Split into multiple transactions |
| **Transact total size** | 4MB | Hard | Reduce item count/size |
| **Provisioned RCU/WCU** | 40K per table (default) | Soft | Request increase (can go to millions) |
| **Table name** | 255 chars | Hard | Fails at creation |
| **Tables per region** | 2500 | Soft | Request increase (can go to 10K+) |
| **Attribute depth** | 32 levels | Hard | Flattens nested structures |
| **Max expression length** | 4KB | Hard | Simplify expressions |
| **DynamoDB Streams** | 24 hours retention | Hard | Data expires after 24h |
| **Global Tables** | 2 replicas per table (default) | Soft | Request increase |

**Critical Interview Edge Cases**:

1. **Batch Write 16MB Limit**:
```python
# BatchWriteItem: Max 25 items OR 16MB total
# Edge case: 20 items × 1MB each = 20MB → EXCEEDS 16MB

# Error: RequestLimitExceeded
# Solution: Split into multiple batches based on SIZE not just count

batch = []
current_size = 0
for item in items:
    item_size = calculate_size(item)  # Must calculate size
    if len(batch) == 25 or current_size + item_size > 16_000_000:
        write_batch(batch)
        batch = []
        current_size = 0
    batch.append(item)
    current_size += item_size
```

2. **Transaction 4MB Total Limit**:
```python
# TransactWriteItems: 100 items OR 4MB total (whichever comes first)

# Scenario: 50 items, each 100KB = 5MB
# Error: ValidationException (exceeds 4MB)

# Must split transaction → loses atomicity!
# Design consideration: Keep transaction items small
```

3. **Query 1MB + FilterExpression**:
```python
# Table: 10K items per partition, each 5KB
# Query: userId = "user123" (returns 2K items = 10MB)
# FilterExpression: status = "active" (matches 100 items)

response = query(
    KeyConditionExpression='userId = :uid',
    FilterExpression='status = :active'
)

# What actually happens:
# 1. DDB scans items until 1MB reached (~200 items)
# 2. Applies FilterExpression (maybe 10 items match)
# 3. Returns ~10 items + LastEvaluatedKey
# 4. Consumed RCUs: 1MB / 4KB = 256 RCUs (NOT 10 items worth!)

# To get all matches:
items = []
last_key = None
while True:
    response = query(..., ExclusiveStartKey=last_key)
    items.extend(response['Items'])
    last_key = response.get('LastEvaluatedKey')
    if not last_key:
        break
```

### Capacity Modes

#### Provisioned Capacity
- Specify **RCU** (Read Capacity Units) and **WCU** (Write Capacity Units)
- **1 RCU** = 1 strongly consistent read/sec OR 2 eventually consistent reads/sec (up to 4KB)
- **1 WCU** = 1 write/sec (up to 1KB)
- **Auto-scaling** available (set min/max, target utilization)
- Cheaper for predictable workloads (can be 5-10× cheaper than on-demand at steady state)

**Detailed Calculation Examples**:

```python
# Example 1: Read 10 items/sec, each 3KB, eventually consistent
# Step 1: Round up each read to 4KB increments
#   3KB → 4KB (rounds up to nearest 4KB)
# Step 2: Calculate RCU per read
#   4KB / 4KB = 1 RCU per read (strongly consistent)
# Step 3: Eventually consistent = half the RCU
#   1 RCU / 2 = 0.5 RCU per read
# Step 4: Total for 10 items/sec
#   10 × 0.5 = 5 RCUs needed

# Example 2: Write 5 items/sec, each 2.5KB
# Step 1: Round up to 1KB increments
#   2.5KB → 3KB (rounds up to nearest 1KB)
# Step 2: Calculate WCU
#   3KB / 1KB = 3 WCUs per write
# Step 3: Total for 5 items/sec
#   5 × 3 = 15 WCUs needed

# Example 3: Read 1 item/sec, 10KB, strongly consistent
# 10KB / 4KB = 2.5 → rounds up to 3 RCUs

# Example 4: Write 1 item/sec, 500 bytes
# 500 bytes → rounds up to 1KB → 1 WCU

# Example 5: Transaction write 3 items (1KB, 2KB, 0.5KB)
# Normal: (1 + 2 + 1) = 4 WCUs
# Transactional: 4 × 2 = 8 WCUs (transactional writes cost 2×)
```

**Burst Capacity** (Often missed in interviews):
- DDB reserves **5 minutes** of unused capacity (up to 300 seconds worth)
- Example: Provisioned 100 RCUs, used 50 RCUs for 5 min
  - Banked: (100 - 50) × 300 sec = 15,000 unused RCUs
  - Can burst to 15,000 RCUs instantly (then throttles)
- **Use case**: Handle short spikes without throttling
- **Not reliable**: Best effort, can't depend on it

#### On-Demand Capacity
- Pay-per-request pricing ($1.25 per million write requests, $0.25 per million read requests)
- No capacity planning needed
- Automatically scales to workload
- **Cost**: ~5-7× more expensive than provisioned at steady state
  - But cheaper for low-traffic or spiky workloads
- **Instant scaling**: From 0 to table peak within minutes
- **Double previous peak**: Can instantly handle 2× previous peak traffic

**Pricing Comparison** (us-east-1):
```python
# Scenario: 1M reads/day (eventually consistent), 100K writes/day

# On-Demand:
# Reads: 1M × $0.25 / 1M = $0.25
# Writes: 100K × $1.25 / 1M = $0.125
# Total: $0.375/day = $11.25/month

# Provisioned (assume reads spread over 12 hours):
# RCU needed: 1M / (12 * 3600) / 2 = ~12 RCUs
# WCU needed: 100K / (12 * 3600) = ~3 WCUs
# Cost: (12 × $0.00013 + 3 × $0.00065) × 730 hours = $2.56/month

# On-Demand is 4.4× more expensive for steady traffic
```

**When to use On-Demand**:
- New tables (unknown traffic patterns)
- Unpredictable workloads (cannot forecast)
- Low-traffic tables (< 1 req/sec average) - provisioned minimum is expensive
- Spiky traffic (10× variance) - burst capacity insufficient

**When to use Provisioned**:
- Predictable traffic patterns
- Steady state > 1 req/sec
- Cost-sensitive (can be 5-7× cheaper)
- Can forecast capacity needs

**Switching modes**:
- Can switch **once per 24 hours** (from provisioned ↔ on-demand)
- Use case: Black Friday (switch to on-demand), then back to provisioned

### Hot Partition Problem (Critical Interview Topic)

**What is it?**
- Uneven distribution of read/write traffic across partitions
- One partition gets disproportionate load → throttling
- **Key insight**: Each partition gets equal share of total RCU/WCU

**How partitioning works internally**:
```python
# Table: 3000 RCUs provisioned, 3 partitions
# Each partition gets: 3000 / 3 = 1000 RCUs

# If one partition receives 1500 RCUs worth of traffic:
# → Partition throttles even though table has 1500 unused RCUs
# → Error: ProvisionedThroughputExceededException
```

**Real-World Causes**:

1. **Poor Partition Key - Date/Time** (Most Common):
```python
# ❌ Bad: Date as partition key
Table(PK: date, SK: orderId)

# Problem:
# - All writes for today go to ONE partition
# - Date "2024-01-15" partition: 10,000 writes/sec → throttles
# - Date "2024-01-14" partition: 0 writes/sec → wasted capacity
# - 99% of partitions idle, 1% overloaded

# ✅ Good: Composite key with high cardinality
Table(PK: customerId, SK: orderDate)
# Distributes across all customers (millions of partition values)
```

2. **Celebrity Problem** (Read Hot Partition):
```python
# Social media: Query posts by userId
Table(PK: userId, SK: postId)

# Normal user: 10 reads/sec
# Celebrity (100M followers): 1M reads/sec

# All reads for celebrity hit ONE partition
# That partition throttles, others idle

# ✅ Solution 1: DAX (in-memory cache)
# - Cache celebrity's posts
# - Reads hit cache (doesn't consume RCU)
# - Sub-millisecond latency

# ✅ Solution 2: ElastiCache/CloudFront
# - Cache at application layer
# - DDB only hit on cache miss
```

3. **Write Sharding for High-Write Items**:
```python
# ❌ Problem: Global counter (all writes to one item)
Item: {PK: "counter", SK: "global", count: 12345}
# 1000 updates/sec to same item → one partition overloaded

# ✅ Solution: Shard writes across N items
Items:
  {PK: "counter#0", SK: "global", count: 1234}
  {PK: "counter#1", SK: "global", count: 1267}
  ...
  {PK: "counter#9", SK: "global", count: 1198}

# Write: random_shard = random(0, 9)
#        increment counter#<random_shard>

# Read total: BatchGet all shards, sum in application
#   total = sum(counter#0..counter#9)

# Result: 1000 writes/sec distributed across 10 partitions = 100/sec each
```

**Temporal Hot Partition**:
```python
# IoT devices: 1M devices report every 5 minutes
# At :00, :05, :10, :15... → massive spike (200K writes/sec)
# Rest of time: idle (100 writes/sec)

# ❌ Provisioned mode: Must provision for peak (200K WCU) → expensive
# ❌ On-demand: Works but expensive for predictable spikes

# ✅ Solutions:
# 1. Jitter: Each device waits random(0-60) seconds before reporting
#    Spreads 200K writes over 1 minute (3,333 writes/sec)
#
# 2. SQS buffer: Devices → SQS → Lambda (writes to DDB at controlled rate)
#    Queue absorbs burst, Lambda processes at steady rate
#
# 3. Kinesis → Lambda: Stream writes, batch to DDB
#    Reduces write amplification
```

**Symptoms & Diagnosis**:

1. **CloudWatch Metrics**:
```
UserErrors (ProvisionedThroughputExceededException) > 0
ConsumedReadCapacityUnits < ProvisionedReadCapacityUnits (capacity unused)

Interpretation: Throttling despite unused capacity = hot partition
```

2. **Contributor Insights**:
- DynamoDB feature: Identifies most accessed partition/sort keys
- Shows: Top N items by request count
- Use: Find "celebrity" items causing hot partitions

**Adaptive Capacity** (DDB's automatic mitigation):
- DDB automatically redirects unused capacity to hot partitions
- **5-30 minutes** to adapt (not instant)
- **Best effort** (can't handle sustained hot partition)
- **Doesn't solve** extreme imbalance (1 partition = 80% of traffic)

**Interview Question**: *"Table has 10K RCUs, getting throttled, CloudWatch shows only 5K RCUs used. Why?"*
```
Answer: Hot partition problem. 10K RCUs distributed across N partitions.
One partition receiving > (10K/N) RCUs → throttles.
Other partitions idle → total shows only 5K used.

Solutions:
1. Add randomness to partition key (better distribution)
2. Use composite key with high cardinality
3. DAX for read-heavy (cache hot items)
4. Write sharding for high-write items
5. On-demand mode (better handles imbalance, but not a fix)
```

### Query vs Scan

#### Query
- **Efficient**: Uses index, returns only matching items
- **Requires**: Partition key value (exact match)
- **Optional**: Sort key condition (range, begins_with, etc.)
- **Supports**: FilterExpression (applied AFTER retrieval, still consumes RCU)
- **Returns**: Items in sort key order
- **Use when**: You know the partition key

```python
# Example: Get all orders for user in date range
query(
    KeyConditionExpression='userId = :uid AND orderDate BETWEEN :start AND :end',
    ProjectionExpression='orderId, totalAmount'  # Only fetch these attributes
)
```

#### Scan
- **Inefficient**: Reads ENTIRE table, evaluates every item
- **No index required**: But examines all data
- **Consumes**: RCUs for ALL scanned data (not just returned items)
- **Parallel scans**: Can divide table into segments, scan in parallel
- **Use when**: Need to examine all items, no suitable query key

**Performance**:
- Scan 1GB table → consumes 1GB / 4KB = 256 RCUs (even if filter returns 1 item)
- Query with PK → only consumes RCUs for returned items

### Read/Write Consumed Units

#### Read Capacity Consumption
- **Strongly consistent read**: Item size / 4KB (rounded up) × 1 RCU
- **Eventually consistent read**: Item size / 4KB (rounded up) × 0.5 RCU
- **Transactional read**: Item size / 4KB (rounded up) × 2 RCU

#### Write Capacity Consumption
- **Standard write**: Item size / 1KB (rounded up) × 1 WCU
- **Transactional write**: Item size / 1KB (rounded up) × 2 WCU

#### Monitoring
- CloudWatch metrics: `ConsumedReadCapacityUnits`, `ConsumedWriteCapacityUnits`
- Response headers: `ConsumedCapacity` (when `ReturnConsumedCapacity=TOTAL`)

### Partitioning Internals
- DynamoDB stores data in **partitions** (10GB each)
- **Partition count** = MAX(RCU/3000, WCU/1000, DataSize/10GB)
- Each partition gets: `TotalRCU / PartitionCount`, `TotalWCU / PartitionCount`
- **Adaptive capacity**: Can temporarily boost hot partitions (5-30 min burst)

### Transactions (ACID Guarantees)

**Guarantees**:
- **Atomicity**: All operations succeed or all fail (no partial commits)
- **Consistency**: Transactions respect item size, capacity limits
- **Isolation**: Serializable isolation (strictest level)
- **Durability**: Persisted across 3 AZs before commit

**Limits**:
- Max **100 items** or **4MB** per transaction (whichever comes first)
- Max **100 unique items** (can operate on same item multiple times)
- Uses **transactional reads/writes** (2× cost: 2 RCU/WCU per KB)
- Supports `TransactWriteItems`, `TransactGetItems`

**TransactWriteItems**:
```python
# Atomically: Deduct inventory + Create order + Update user balance
response = transact_write_items(
    TransactItems=[
        {
            'Update': {
                'TableName': 'Inventory',
                'Key': {'productId': '12345'},
                'UpdateExpression': 'SET quantity = quantity - :qty',
                'ConditionExpression': 'quantity >= :qty',  # Prevent oversell
                'ExpressionAttributeValues': {':qty': 5}
            }
        },
        {
            'Put': {
                'TableName': 'Orders',
                'Item': {'orderId': '99', 'productId': '12345', 'qty': 5}
            }
        },
        {
            'Update': {
                'TableName': 'Users',
                'Key': {'userId': 'user_456'},
                'UpdateExpression': 'SET balance = balance - :cost',
                'ConditionExpression': 'balance >= :cost',  # Sufficient funds
                'ExpressionAttributeValues': {':cost': 100}
            }
        }
    ]
)

# If ANY ConditionExpression fails → entire transaction rolls back
# Error: TransactionCanceledException (with cancellation reasons)
```

**Critical Edge Cases**:

1. **Idempotency Token** (10-minute window):
```python
# Transaction with same ClientRequestToken within 10 minutes
# → Treated as duplicate (returns success without re-executing)

transact_write_items(
    ClientRequestToken='unique-id-12345',  # Idempotency key
    TransactItems=[...]
)

# Use case: Retry failed transaction (network error)
# Without token: Might execute twice (double charge!)
# With token: Safe to retry (executes once)
```

2. **Transaction Conflicts** (Optimistic Locking):
```python
# Two transactions update same item concurrently
# Transaction A: Update inventory quantity = 10 - 5 = 5
# Transaction B: Update inventory quantity = 10 - 3 = 7

# Execution:
# 1. Both read quantity = 10
# 2. Both try to write
# 3. One succeeds (say A, sets quantity = 5)
# 4. Other fails: TransactionCanceledException
#    Reason: "ConditionalCheckFailed" (version mismatch)

# B must retry:
# 1. Re-read quantity = 5
# 2. Write quantity = 5 - 3 = 2
```

3. **Cost Calculation**:
```python
# Transactional writes: 2× WCU
# Transactional reads: 2× RCU

# Example: TransactWriteItems with 3 items (1KB, 2KB, 3KB)
# Normal write WCU: 1 + 2 + 3 = 6 WCUs
# Transactional: 6 × 2 = 12 WCUs

# 100-item transaction (all 1KB):
# 100 items × 1 WCU × 2 = 200 WCUs consumed
```

4. **Table/Index Writes Count**:
```python
# Transaction writes to table WITH 2 GSIs
# Each write to base table → also writes to 2 GSIs

# Transaction: Write 1 item (1KB)
# Base table: 1 WCU × 2 (transactional) = 2 WCUs
# GSI 1: 1 WCU × 2 = 2 WCUs
# GSI 2: 1 WCU × 2 = 2 WCUs
# Total: 6 WCUs (3 writes × transactional cost)
```

**When NOT to use transactions**:
- **Single item** updates (use UpdateItem with ConditionExpression - no 2× cost)
- **High-throughput** (cost doubles)
- **Loosely related items** (eventual consistency acceptable)

**When to use transactions**:
- **Financial operations** (payments, transfers) - atomicity critical
- **Inventory management** (prevent overselling)
- **Cross-table consistency** (user signup: create user + create profile + init settings)
- **Complex conditions** (multi-item constraints)

### Time-to-Live (TTL) - Auto-Delete Expired Items

**How it works**:
- Automatically delete expired items **(no WCU cost - completely free)**
- Set **epoch timestamp** (Unix time in seconds) in designated TTL attribute
- Deletion happens within **48 hours** of expiry (not immediate, not guaranteed)
- Deleted items appear in DynamoDB Streams (can archive to S3)

**Critical Details**:

1. **Not Immediate** (Common Interview Trap):
```python
# Item with TTL: { userId: "123", expireAt: 1705305600 }
# expireAt = 2024-01-15 10:00:00 AM (epoch timestamp)

# Actual deletion: Between 10:00 AM - 48 hours later
# Could be deleted: 2024-01-15 10:01 AM OR 2024-01-17 10:00 AM

# ❌ Cannot rely on TTL for immediate deletion
# ❌ Don't use TTL for access control (expired items still readable)

# ✅ Application must check TTL:
current_time = int(time.time())
if item.get('expireAt', float('inf')) > current_time:
    # Item valid
else:
    # Item expired (treat as deleted)
```

2. **No WCU Consumption**:
```python
# 1M items with TTL expire today
# Deletion cost: $0 (no WCU consumed)

# vs manual delete:
# BatchWriteItem 1M items: ~1M WCUs = $650/hour (at 1M WCU/hour)

# TTL saves massive cost for time-based data expiration
```

3. **TTL Attribute Format**:
```python
# ✅ Correct: Epoch timestamp (seconds since 1970-01-01)
{
  "userId": "123",
  "sessionData": "...",
  "expireAt": 1705305600  # Number type, epoch seconds
}

# ❌ Wrong: ISO string, milliseconds, not a number
"expireAt": "2024-01-15T10:00:00Z"  # DDB ignores, never deletes
"expireAt": 1705305600000           # Milliseconds (year 56000+!)
```

4. **DynamoDB Streams + TTL**:
```python
# Deleted items appear in stream with:
# eventName: "REMOVE"
# userIdentity: "dynamodb.amazonaws.com" (not your app)

# Use case: Archive expired sessions to S3 before deletion
# Lambda triggered by stream → S3.put_object()
```

**Common Use Cases**:

1. **Session Storage** (30-minute sessions):
```python
# Write session:
put_item(Item={
    'sessionId': uuid(),
    'userId': '123',
    'data': {...},
    'expireAt': int(time.time()) + 1800  # 30 min from now
})

# TTL auto-deletes after session expires (within 48h)
# No manual cleanup needed
```

2. **Temporary Data** (verification codes, password resets):
```python
# Verification code expires in 10 minutes:
{
  'code': '123456',
  'email': 'user@example.com',
  'expireAt': int(time.time()) + 600  # 10 min
}

# Application checks expireAt before validating code
# TTL cleans up old codes automatically
```

3. **Event Data / Logs** (retain 90 days):
```python
# IoT events, access logs
{
  'eventId': uuid(),
  'timestamp': 1705305600,
  'data': {...},
  'expireAt': 1705305600 + (90 * 86400)  # 90 days later
}

# Auto-delete old logs, save storage cost
```

**Monitoring**:
```python
# CloudWatch Metrics:
# - DeletionCount: Items deleted by TTL
# - SuccessfulRequestLatency: TTL scan latency

# Stream records:
# Count DELETE events from "dynamodb.amazonaws.com"
# Track: What's being deleted, how much data
```

**Cost Savings Example**:
```python
# 100GB table, 50% data expires monthly
# Without TTL:
# - Manual deletion: 50GB deleted = ~50M items × 1 WCU = $32.50/month
# - Storage: 100GB × $0.25/GB = $25/month
# Total: $57.50/month

# With TTL:
# - Deletion: $0 (free)
# - Storage stabilizes: 50GB × $0.25 = $12.50/month
# Total: $12.50/month (78% savings!)
```

### DynamoDB Streams (Change Data Capture)

**Core Concepts**:
- **Change data capture**: Ordered stream of Insert, Modify, Delete events
- **Retention**: 24 hours (hard limit - data expires after 24h)
- **Guaranteed order**: Within same partition key (across partition: eventual)
- **Exactly once**: Each change appears exactly once in stream
- **Near real-time**: Typically < 1 second latency

**Stream View Types**:
| View Type | Contains | Use Case | Size Impact |
|-----------|----------|----------|-------------|
| `KEYS_ONLY` | PK + SK only | Lightweight triggers, just need to know what changed | Smallest |
| `NEW_IMAGE` | Full item after change | Most common, sync to other systems | Medium |
| `OLD_IMAGE` | Full item before change | Audit trail, undo operations | Medium |
| `NEW_AND_OLD_IMAGES` | Before + after | Detailed audit, diff calculation | Largest (2× data) |

**Critical Limitations**:

1. **24-Hour Retention** (Cannot extend):
```python
# Stream enabled at 2024-01-15 10:00 AM
# Record created: 2024-01-15 11:00 AM
# Record expires: 2024-01-16 11:00 AM (24h later)

# If Lambda fails to process:
# - Retry for 24 hours
# - After 24h: Record lost forever (cannot replay)

# ✅ Solution: Enable Kinesis Data Streams for DDB (unlimited retention)
```

2. **Shard Limits** (similar to Kinesis):
```python
# Each stream shard:
# - 1000 records/sec
# - 1MB/sec

# High-write table (10K writes/sec):
# - Needs ~10 stream shards
# - DDB auto-manages shards
# - No manual provisioning
```

3. **Stream Records != Table Writes** (Deduplication):
```python
# Multiple updates to same item within ~1 second
# → May be combined into 1 stream record

# Write 1: {userId: "123", status: "pending"}
# Write 2: {userId: "123", status: "active"}   (within 1 sec)
# Write 3: {userId: "123", status: "verified"} (within 1 sec)

# Stream might show:
# 1 record: OLD_IMAGE: {status: "pending"}, NEW_IMAGE: {status: "verified"}
# (skipped intermediate state "active")

# ✅ Solution: If intermediate states matter, add timestamp or sequence field
```

**Lambda Integration** (Most Common Pattern):
```python
# Lambda triggered by stream
# Event batch: Up to 10,000 records or 6MB

def lambda_handler(event, context):
    for record in event['Records']:
        if record['eventName'] == 'INSERT':
            new_item = record['dynamodb']['NewImage']
            # Sync to Elasticsearch, send email, etc.
        elif record['eventName'] == 'MODIFY':
            old = record['dynamodb']['OldImage']
            new = record['dynamodb']['NewImage']
            # Detect specific field changes
        elif record['eventName'] == 'REMOVE':
            old_item = record['dynamodb']['OldImage']
            # Archive deleted items to S3

# Failures:
# - Lambda retries failed records (exponential backoff)
# - After 24h or max retries → send to DLQ (SQS/SNS)
# - Bisect batch on error (helps isolate poison pill)
```

**Kinesis Data Streams for DynamoDB** (Extended Retention):
```python
# DynamoDB → Kinesis Data Streams (not DDB Streams)
# Benefits:
# - Retention: 1-365 days (vs 24h for DDB Streams)
# - Multiple consumers: Fan-out to many apps
# - Kinesis Analytics: SQL queries on change stream
# - Better for complex stream processing

# Enable:
aws dynamodb enable-kinesis-streaming-destination \
  --table-name MyTable \
  --stream-arn arn:aws:kinesis:region:account:stream/MyStream

# Cost: $0.10 per GB replicated (in addition to table costs)
```

**Common Patterns**:

1. **Materialized View** (Aggregate Table):
```python
# Orders table: { orderId, userId, amount, date }
# Maintain aggregated: UserTotals { userId, totalSpent, orderCount }

# DDB Stream → Lambda:
# INSERT order → Update UserTotals (increment totalSpent, orderCount)
# DELETE order → Update UserTotals (decrement)
```

2. **Cross-Region Replication** (Global Tables use this internally):
```python
# DDB Streams → Lambda → Write to DDB in another region
# Manual replication (Global Tables automate this)
```

3. **Search Index Sync** (DDB → Elasticsearch/OpenSearch):
```python
# DDB Streams → Lambda → Index to OpenSearch
# Keep search index in sync with DDB
# Query: OpenSearch (full-text), fetch details: DDB (by ID)
```

4. **Audit Log / Data Lake**:
```python
# DDB Streams → Kinesis Firehose → S3 (Parquet)
# All changes archived to S3 for compliance, analytics
```

**Monitoring**:
```python
# CloudWatch Metrics:
# - IteratorAge: How far behind consumers are (< 1s is good)
# - ReturnedRecordsCount: Records delivered

# If IteratorAge growing → consumer can't keep up
# Solutions:
# 1. Increase Lambda concurrency
# 2. Optimize Lambda (faster processing)
# 3. Batch processing (process multiple records efficiently)
```

### Best Practices
1. **Partition key**: High cardinality, even access distribution
2. **Keep items small**: < 400KB (soft limit 400KB, hard limit 400KB)
3. **Use projections**: Only copy needed attributes to GSIs
4. **Batch operations**: `BatchGetItem`, `BatchWriteItem` (up to 25 items)
5. **Exponential backoff**: Retry throttled requests with backoff
6. **Avoid scans**: Design keys to enable queries

### Limits (Key Ones)
- **Item size**: 400KB (includes attribute names + values)
- **Partition key**: 2048 bytes max
- **Sort key**: 1024 bytes max
- **GSI**: 20 per table (soft limit)
- **LSI**: 5 per table (hard limit)
- **Batch operations**: 25 items max
- **Transaction size**: 100 items or 4MB

---

## S3 (Simple Storage Service)

### Core Concepts
- **Object storage**: Store files (objects) in buckets (not file system, not block storage)
- **Durability**: 99.999999999% (11 nines) — lose 1 object per 10 billion per 10,000 years
- **Availability**: 99.99% (Standard) = ~53 min downtime/year, 99.9% (IA) = ~8.7 hours/year
- **Scalability**: Unlimited storage, unlimited objects per bucket
- **Object size**: 0 bytes to 5TB per object (single file)
- **Regional service**: Buckets tied to region (but globally unique names)

### S3 Hard Limits & Critical Edge Cases

| Limit | Value | Type | Impact | Workaround |
|-------|-------|------|--------|------------|
| **Object size** | 5TB | Hard | Single file max | None (5TB is max) |
| **PUT single upload** | 5GB | Hard | Fails if file > 5GB | Use multipart upload |
| **Bucket name** | 3-63 chars, globally unique | Hard | Creation fails if taken | Choose unique name |
| **Buckets per account** | 10,000 (hard limit) | Hard | Cannot create more than 10,000 | Use prefixes within buckets instead |
| **Object key length** | 1024 bytes (UTF-8) | Hard | Long paths fail | Shorten paths |
| **Multipart upload parts** | 10,000 parts max | Hard | Max parts per upload | Each 5MB-5GB (50TB math, but 5TB object limit enforced) |
| **Request rate** | 3,500 PUT/s, 5,500 GET/s per prefix | Soft | Throttling (503) | Use more prefixes |
| **List objects** | 1000 objects per call | Hard | Must paginate | Use pagination tokens |
| **Metadata size** | 2KB per object (user + system) | Hard | Metadata exceeds limit | Store large metadata in object |
| **Tags per object** | 10 tags | Hard | Cannot add more | Use metadata or external DB |
| **Lifecycle rules** | 1000 per bucket | Hard | Cannot add more | Consolidate rules |
| **Event notifications** | No hard limit | Soft | Performance degrades | Use EventBridge for complex routing |
| **Versioning** | Unlimited versions | None | Storage cost multiplies | Use lifecycle to delete old versions |
| **Batch operations** | 1B objects per job | Soft | Job fails | Split into multiple jobs |

**Interview Edge Cases**:

#### 1. **5GB PUT Limit** (Very Common Mistake):
```python
# ❌ Fails: Upload 10GB file with PUT
s3.put_object(Bucket='my-bucket', Key='large-file.zip', Body=open('10GB.zip'))
# Error: EntityTooLarge (max 5GB for PUT)

# ✅ Required: Multipart upload for > 5GB
s3.create_multipart_upload(Bucket='my-bucket', Key='large-file.zip')
# Upload parts (each 5MB-5GB), max 10,000 parts
# Complete multipart upload

# Recommended: Use multipart for files > 100MB (faster, resumable)
```

#### 2. **Request Rate Limits** (3,500 PUT, 5,500 GET per prefix):
```python
# Prefix = path between bucket and object name
# s3://bucket/2024/01/15/file.jpg → prefix = "2024/01/15/"
# s3://bucket/user123/photo.jpg   → prefix = "user123/"

# ❌ Problem: All 10K req/sec to one prefix
s3://logs/app.log.1
s3://logs/app.log.2
# All in "logs/" prefix → 3,500 PUT/s limit → throttling (503 SlowDown)

# ✅ Solution: Distribute across prefixes (add randomness/hash)
s3://logs/a1b2/app.log.1  # prefix = "logs/a1b2/"
s3://logs/c3d4/app.log.2  # prefix = "logs/c3d4/"
s3://logs/e5f6/app.log.3  # prefix = "logs/e5f6/"

# 100 prefixes × 3,500 PUT/s = 350,000 PUT/s total throughput
```

#### 3. **Consistency Guarantees** (Since Dec 2020):
```python
# Strong read-after-write consistency (applies to ALL operations)

# Write new object:
s3.put_object(Bucket='bucket', Key='file.txt', Body='data')
# Immediately read:
obj = s3.get_object(Bucket='bucket', Key='file.txt')
# ✅ Guaranteed to see latest write (no eventual consistency lag)

# Overwrite object:
s3.put_object(Key='file.txt', Body='new data')  # Overwrite
obj = s3.get_object(Key='file.txt')
# ✅ Returns 'new data' immediately

# Delete object:
s3.delete_object(Key='file.txt')
s3.get_object(Key='file.txt')
# ✅ Immediately returns NoSuchKey error

# List objects:
s3.list_objects_v2(Bucket='bucket')
# ✅ Reflects latest changes immediately

# Before Dec 2020: Eventually consistent (1-2 sec lag)
```

### Object Structure
```
s3://bucket-name/key
├── Bucket: Global namespace (must be unique across ALL AWS accounts)
└── Key: Object path (e.g., "folder/subfolder/file.txt")
     ├── Object data (the file content)
     ├── Metadata (system + user-defined)
     ├── Version ID (if versioning enabled)
     └── Access control (ACL, bucket policy)
```

### Storage Classes

| Class | Use Case | Availability | Min Storage | Retrieval Fee |
|-------|----------|--------------|-------------|---------------|
| **Standard** | Frequently accessed | 99.99% | None | No |
| **Intelligent-Tiering** | Unknown/changing patterns | 99.9% | None | Small monitoring fee |
| **Standard-IA** | Infrequent access | 99.9% | 30 days | Yes |
| **One Zone-IA** | Non-critical, infrequent | 99.5% | 30 days | Yes |
| **Glacier Instant** | Archive, instant retrieval | 99.9% | 90 days | Yes |
| **Glacier Flexible** | Archive, mins-hours retrieval | 99.99% | 90 days | Yes |
| **Glacier Deep Archive** | Long-term archive (7-10 years) | 99.99% | 180 days | Yes |

**Lifecycle Policies**: Auto-transition objects between classes based on age

### Consistency Model
- **Strong read-after-write consistency** (since Dec 2020)
- Immediately consistent for:
  - New PUTs: Read immediately after write
  - Overwrites: Get latest version immediately
  - Deletes: Object disappears immediately
  - List operations: Reflect latest changes

### Performance

#### Request Rate
- **3,500 PUT/COPY/POST/DELETE** per second per prefix
- **5,500 GET/HEAD** per second per prefix
- **Prefix**: Path between bucket and object name
  - `s3://bucket/folder1/file.txt` → prefix = `folder1/`
  - More prefixes = more throughput (partition parallelism)

#### Optimization Techniques

**1. Multipart Upload** (Critical for interviews):
- **Required** for objects > 5GB (PUT fails otherwise)
- **Recommended** for objects > 100MB (3-5× faster, resumable)
- Upload parts in parallel → faster upload, can resume failed uploads
- Max **10,000 parts** per upload, each part 5MB-5GB (last part can be < 5MB)

**Detailed Mechanics**:
```python
# Calculate optimal part size:
# Goal: Balance between part count and size
file_size = 10 * 1024**3  # 10GB
ideal_parts = 100  # Sweet spot for parallelism
part_size = file_size // ideal_parts  # 100MB per part

# Step 1: Initiate multipart upload
response = s3.create_multipart_upload(
    Bucket='my-bucket',
    Key='large-file.zip',
    StorageClass='INTELLIGENT_TIERING',  # Optional
    ServerSideEncryption='AES256'         # Optional
)
upload_id = response['UploadId']  # Critical: Store this for resume

# Step 2: Upload parts (parallel)
from concurrent.futures import ThreadPoolExecutor

def upload_part(part_num, data):
    return s3.upload_part(
        Bucket='my-bucket',
        Key='large-file.zip',
        UploadId=upload_id,
        PartNumber=part_num,      # 1-indexed (1 to 10,000)
        Body=data
    )

with ThreadPoolExecutor(max_workers=10) as executor:
    futures = []
    with open('10GB.zip', 'rb') as f:
        for part_num in range(1, 101):
            data = f.read(part_size)
            future = executor.submit(upload_part, part_num, data)
            futures.append((part_num, future))

    # Collect results
    parts = []
    for part_num, future in futures:
        result = future.result()
        parts.append({
            'PartNumber': part_num,
            'ETag': result['ETag']  # Required for complete
        })

# Step 3: Complete (atomic operation)
s3.complete_multipart_upload(
    Bucket='my-bucket',
    Key='large-file.zip',
    UploadId=upload_id,
    MultipartUpload={'Parts': sorted(parts, key=lambda x: x['PartNumber'])}
)
```

**Critical Gotchas & Edge Cases**:

a) **Incomplete Multipart Uploads** (Storage Leak - Very Common!):
```python
# Problem: If upload crashes before complete_multipart_upload:
# - Uploaded parts remain in S3 (invisible in list_objects)
# - You're charged for storage indefinitely
# - No automatic cleanup!

# Example: Upload 1000 files/day, 50% fail midway
# After 30 days: 15,000 incomplete uploads × 500MB = 7.5TB wasted!

# ✅ Solution 1: Lifecycle policy (auto-abort after N days)
{
  "Rules": [{
    "Id": "AbortIncompleteMultipart",
    "Status": "Enabled",
    "AbortIncompleteMultipartUpload": {
      "DaysAfterInitiation": 7  # Delete incomplete after 7 days
    }
  }]
}

# ✅ Solution 2: Cleanup script (list + abort)
uploads = s3.list_multipart_uploads(Bucket='bucket')
for upload in uploads.get('Uploads', []):
    # Abort uploads older than 24 hours
    if upload['Initiated'] < datetime.now() - timedelta(hours=24):
        s3.abort_multipart_upload(
            Bucket='bucket',
            Key=upload['Key'],
            UploadId=upload['UploadId']
        )
```

b) **Part Size Constraints**:
```python
# Min part size: 5MB (except last part - can be any size)
# Max part size: 5GB
# Max parts: 10,000
# Max object: 5TB (even with multipart)

# ❌ Error: 1TB file, 1MB parts
# 1TB / 1MB = 1,000,000 parts (exceeds 10,000 limit)
# Error: InvalidRequest

# ❌ Error: Parts too small
# Part 1-99: < 5MB each → Error: EntityTooSmall
# Only last part can be < 5MB!

# ✅ Correct sizing:
# 5TB max object / 10,000 parts = 512MB minimum part size for max file
# Recommendation: 100MB - 500MB parts for most use cases
```

c) **ETag Behavior** (Interview Trap):
```python
# Single PUT: ETag = MD5 hash
# Multipart: ETag = <complex_hash>-<part_count>

# Example:
s3.put_object(Key='file.txt', Body=data)
# ETag: "5d41402abc4b2a76b9719d911017c592" (MD5)

# vs
# Multipart upload (100 parts)
# ETag: "a1b2c3d4e5f6-100"
#                    ^^^^ number of parts

# ❌ Cannot use multipart ETag for MD5 validation
# ✅ Use S3 checksum (SHA256) or calculate MD5 per-part
```

d) **Failure & Resume Strategy**:
```python
# Store state in DDB or local file:
state = {
    'upload_id': 'abc123',
    'completed_parts': [1, 2, 3, 5, 7],  # Missing: 4, 6, 8+
    'total_parts': 100
}

# On resume:
# 1. List parts already uploaded
response = s3.list_parts(
    Bucket='bucket',
    Key='file.zip',
    UploadId=state['upload_id']
)
uploaded = {p['PartNumber'] for p in response['Parts']}

# 2. Upload only missing parts
for part_num in range(1, 101):
    if part_num not in uploaded:
        upload_part(part_num, ...)

# 3. Complete with ALL parts (including previously uploaded)
```

**2. Transfer Acceleration**:
   - Route uploads through CloudFront edge locations
   - 50-500% faster for long-distance uploads
   - Uses `bucket-name.s3-accelerate.amazonaws.com` endpoint

3. **S3 Select**:
   - Query data inside objects using SQL
   - Retrieve subset of data (reduces transfer, cost)
   - Works with CSV, JSON, Parquet
   - Up to **400% faster**, **80% cheaper** than retrieving full object

4. **Byte-Range Fetches**:
   - Download specific byte ranges in parallel
   - Faster for large files, can resume failed downloads

### Versioning
- **Track all versions** of an object (every PUT creates new version)
- **Cannot disable** once enabled (only suspend)
- **Delete marker**: Deleting object adds marker (can undelete by removing marker)
- **Version ID**: Each version gets unique ID
- **MFA Delete**: Require MFA to permanently delete version (extra protection)

### Encryption

#### Server-Side Encryption (SSE)
1. **SSE-S3**: S3-managed keys (AES-256)
   - Default, automatic, no extra cost
   - Header: `x-amz-server-side-encryption: AES256`

2. **SSE-KMS**: AWS KMS-managed keys
   - More control, audit trail (CloudTrail)
   - **KMS quota**: API calls count toward KMS limits (5,500-30,000 req/sec)
   - Header: `x-amz-server-side-encryption: aws:kms`

3. **SSE-C**: Customer-provided keys
   - You manage keys, S3 manages encryption
   - Must provide key with every request (HTTPS only)

#### Client-Side Encryption
- Encrypt data before uploading
- You manage keys and encryption process

### Access Control

#### Mechanisms (Evaluated in Order)
1. **IAM Policies**: Attached to users/roles (identity-based)
2. **Bucket Policies**: Attached to bucket (resource-based, JSON)
   - Can grant cross-account access
   - Can restrict by IP, VPC endpoint, time, etc.
3. **Access Control Lists (ACLs)**: Legacy, per-object/bucket
   - Limited controls (READ, WRITE, FULL_CONTROL)
   - Discouraged (use bucket policies instead)
4. **S3 Block Public Access**: Override all other settings (safety net)

#### Pre-Signed URLs
- Temporary URL granting time-limited access
- Generated using IAM credentials (SDK/CLI)
- Expiration: 1 second - 7 days (AWS SigV4)
- Use case: Allow users to upload/download without AWS credentials

### Replication

#### Cross-Region Replication (CRR)
- Replicate objects to **different region**
- Use case: Compliance, latency reduction, DR

#### Same-Region Replication (SRR)
- Replicate within **same region** (different bucket)
- Use case: Log aggregation, production-test sync

**Requirements**:
- Versioning enabled on both buckets
- IAM role with replication permissions
- **Not retroactive**: Only replicates new objects (use S3 Batch Replication for existing)
- Can replicate metadata, tags, ACLs, object lock settings

### Event Notifications
- Trigger events on object creation, deletion, restoration
- **Targets**: SNS, SQS, Lambda, EventBridge
- **Filters**: By prefix, suffix (e.g., `*.jpg`)
- **EventBridge**: More advanced routing, multiple targets, filtering

### S3 Object Lock
- **WORM** (Write Once Read Many) model
- **Modes**:
  1. **Governance**: Can be overridden with special permissions
  2. **Compliance**: Cannot be overridden (even by root), for regulations
- **Retention period**: Fixed duration (days/years)
- **Legal hold**: Indefinite lock (no expiration)
- **Requires**: Versioning enabled

### Limits (Key Ones)
- **Bucket name**: 3-63 chars, lowercase, globally unique
- **Buckets per account**: 10,000 (hard limit, increased from 100 in 2024)
- **Object size**: Max 5TB
- **Single PUT**: Max 5GB (use multipart for larger)
- **Multipart parts**: Max 10,000 parts per upload

### Best Practices
1. **Use prefixes**: Distribute objects across prefixes for higher throughput
2. **Multipart upload**: For objects > 100MB
3. **Lifecycle policies**: Auto-transition to cheaper storage classes
4. **CloudFront**: Cache frequently accessed objects at edge
5. **S3 Select**: Query only needed data (reduce cost/time)
6. **Versioning + MFA Delete**: Protect critical data
7. **Bucket policies**: Use instead of ACLs
8. **Encryption**: Enable default encryption (SSE-S3)

### Common Patterns in System Design
- **Data lake**: Store raw data (Parquet, JSON, CSV) → query with Athena
- **Static website hosting**: Host HTML/CSS/JS files
- **Backup/archive**: Lifecycle → Glacier for long-term retention
- **Log aggregation**: CloudTrail, VPC Flow Logs, app logs → S3 → Athena
- **Event-driven**: S3 → Lambda → process uploaded files

---

## Kinesis

### Overview
- **Real-time streaming** data platform (like Apache Kafka)
- **Sub-second latency**: Typically 70-200ms (vs SQS ~1 sec)
- Three services:
  1. **Kinesis Data Streams**: Real-time data ingestion and processing (core service)
  2. **Kinesis Data Firehose**: Load streams into S3, Redshift, Elasticsearch (ETL)
  3. **Kinesis Data Analytics**: SQL queries on streaming data (real-time analytics)

---

### Kinesis Data Streams

#### Core Concepts
```
Producer → Shard(s) → Consumer(s)
           ├── Partition Key (determines shard via MD5 hash)
           ├── Sequence Number (ordering within shard, monotonic)
           └── Data Blob (up to 1MB)
```

### Kinesis Hard Limits & Critical Constraints

| Limit | Value | Type | Impact | Solution |
|-------|-------|------|--------|----------|
| **Record size** | 1MB | Hard | Larger records fail | Split record or use S3 pointer |
| **Write per shard** | 1MB/s OR 1000 rec/s | Hard | Throttling (ProvisionedThroughputExceeded) | Add shards, better partition key |
| **Read per shard** | 2MB/s (shared) | Hard | Slow consumers | Enhanced fan-out (2MB/s per consumer) |
| **Read transactions** | 5/sec per shard | Hard | GetRecords throttling | Use enhanced fan-out or batch reads |
| **Retention** | 24h (default), 365d (max) | Configurable | Data expires | Increase retention or archive to S3 |
| **Shard limit** | 500 shards (default) | Soft | Cannot scale beyond | Request increase (up to thousands) |
| **PutRecords batch** | 500 records or 5MB | Hard | Must batch | Split into multiple requests |
| **Enhanced fan-out** | 20 consumers per stream | Hard | Cannot add more | Use classic or redesign |
| **Partition key** | 256 bytes | Hard | Hash determines shard | Keep keys small |
| **Sequence number** | 128-bit string | N/A | Used for ordering, dedup | Auto-generated |

#### Shards (Fundamental Unit)
- **Shard**: Base throughput unit (immutable, like Kafka partition)
- **Capacity per shard**:
  - **Write**: 1MB/sec OR 1,000 records/sec (whichever comes first)
  - **Read (Shared)**: 2MB/sec total OR 5 GetRecords/sec (shared across ALL consumers)
  - **Read (Enhanced)**: 2MB/sec per consumer (dedicated pipe)

**Critical Write Limit Example**:
```python
# Shard write limit: 1MB/s OR 1000 rec/s

# Scenario 1: 2000 records/sec, each 100 bytes
# Total: 2000 × 100 = 200KB/s (< 1MB/s ✅)
# But: 2000 rec/s > 1000 rec/s ❌
# Result: Throttled at 1000 rec/s

# Scenario 2: 500 records/sec, each 3KB
# Total: 500 × 3KB = 1.5MB/s (> 1MB/s ❌)
# Result: Throttled at ~333 rec/s (1MB / 3KB)

# ✅ Solution: Add more shards
# Need 2000 rec/s → minimum 2 shards (2× capacity)
```

**Read Limit Example (Shared Fan-Out)**:
```python
# One shard: 2MB/s read, 5 GetRecords/sec

# 3 consumers reading from same shard:
# Consumer A: GetRecords every 1 sec → reads ~660KB/s (2MB/3)
# Consumer B: GetRecords every 1 sec → reads ~660KB/s
# Consumer C: GetRecords every 1 sec → reads ~660KB/s

# Total: 2MB/s shared (each gets 1/3)

# If Consumer A reads faster (5 GetRecords/sec):
# → Uses up all 5 transactions/sec
# → Consumers B & C throttled!

# ✅ Solution: Enhanced fan-out (each gets dedicated 2MB/s)
```

#### Shard Scaling (Manual Resharding)
- **Scaling**: Add/remove shards manually (no auto-scaling in provisioned mode)
  - **Split**: Divide one shard into two (increase capacity, costs 2×)
  - **Merge**: Combine two shards into one (reduce cost, halves capacity)
  - Takes a few seconds, stream remains available (no downtime)

**Resharding Process** (Interview Detail):
```python
# Initial: 1 shard, 1MB/s write capacity
# Need: 5MB/s write capacity → split into 5 shards

# Step 1: Split shard-0 into shard-1 and shard-2
kinesis.split_shard(
    StreamName='my-stream',
    ShardToSplit='shardId-000000000000',
    NewStartingHashKey='170141183460469231731687303715884105728'  # Midpoint
)

# Result:
# - shard-0: CLOSED (no new writes, existing data still readable)
# - shard-1: OPEN (handles hash range [0 - midpoint))
# - shard-2: OPEN (handles hash range [midpoint - max))

# Step 2: Repeat for shards 1 and 2 (to get to 5 total)

# Important:
# - Old shard remains for retention period (can still read old data)
# - Consumers must track parent/child relationships
# - Resharding costs: No additional charge, just pay for shard-hours
```

#### Partition Key (Critical for Performance)
- **Determines shard assignment**: MD5(partition_key) → 128-bit hash → maps to shard
- **Ordering guarantee**: All records with same partition key → same shard → ordered by sequence number
- **Hot shard risk**: If partition key has low cardinality → all traffic to few shards
- **Best practice**: High cardinality key (unique user ID, random GUID, device ID)

**Partition Key Deep Dive**:

**How Shard Assignment Works**:
```python
# Hash space: 0 to 2^128-1 (340 undecillion)
# Split evenly across shards

# Example: 3 shards
# Shard 0: hash range [0 - 113,427,455,640,312,821,154...]
# Shard 1: hash range [113,427... - 226,854...]
# Shard 2: hash range [226,854... - 340,282...]

# Put record with partition key "user123"
hash = MD5("user123") = 0xA1B2C3D4E5F6... (128-bit number)
# Maps to one of the 3 shards based on hash range

# Critical: Same partition key ALWAYS goes to same shard
# → Ordered processing guaranteed
# → But can create hot shard if key poorly chosen
```

**Hot Shard Problem** (Very Common Interview Topic):
```python
# ❌ Bad: Low-cardinality partition key
# IoT devices send metrics:
put_record(
    StreamName='metrics',
    PartitionKey='metrics',  # ALL records same key!
    Data=json.dumps({...})
)

# Result:
# - All 10K rec/s go to ONE shard
# - That shard: 1MB/s or 1K rec/s limit → THROTTLED
# - Other shards: IDLE (wasted capacity)
# - Error: ProvisionedThroughputExceededException

# ✅ Good: High-cardinality key
put_record(
    StreamName='metrics',
    PartitionKey=device_id,  # Millions of unique device IDs
    Data=json.dumps({...})
)

# Result:
# - Traffic distributed evenly across all shards
# - Each shard gets ~(total traffic / num_shards)
# - No throttling, optimal utilization
```

**Partition Key Strategies**:

1. **Random (Max Distribution)**:
```python
import uuid
put_record(
    PartitionKey=str(uuid.uuid4()),  # Every record different shard
    Data=data
)

# Pros: Perfect distribution, no hot shards
# Cons: No ordering (each record different shard)
# Use case: Order doesn't matter, max throughput
```

2. **Entity ID (Ordered by Entity)**:
```python
put_record(
    PartitionKey=f'user_{user_id}',  # All user's events → same shard
    Data=data
)

# Pros: Events for same user ordered
# Cons: Hot user → hot shard (celebrity problem)
# Use case: User activity streams, order matters per user
```

3. **Composite with Shard Count**:
```python
# Limit shards per entity (prevent one entity from monopolizing shard)
shard_count = 100
partition_key = f'user_{user_id}#{hash(user_id) % shard_count}'

put_record(PartitionKey=partition_key, Data=data)

# Pros: Distributes even hot users across N shards
# Cons: Lose ordering within entity (split across shards)
# Use case: High-traffic entities, acceptable to lose per-entity ordering
```

4. **Explicit Shard** (Advanced):
```python
# Control exactly which shard (use ExplicitHashKey)
shard_id = 0  # Target specific shard
hash_key_range_start = 0
hash_key_range_end = 2**128 // num_shards

put_record(
    PartitionKey='any',  # Ignored when ExplicitHashKey provided
    ExplicitHashKey=str(hash_key_range_start + 1),  # Targets shard 0
    Data=data
)

# Pros: Full control over shard assignment
# Cons: Complex, must manage hash ranges manually
# Use case: Testing, debugging, custom shard routing
```

**Interview Question**: *"10K devices send 1 msg/sec. Need ordering per device. How to design?"*
```
Answer:
- Partition key = device_id (high cardinality)
- 10K devices × 1 msg/s = 10K msg/s total
- 1 shard = 1K msg/s → Need 10 shards minimum
- Each device's messages go to same shard (ordered)
- 10K devices distributed across 10 shards (~1K devices per shard)
- Result: Ordered per device, no hot shards (assuming even device distribution)

Edge case: If 1 device sends 2K msg/s (hot device)
- That device monopolizes 1 shard → throttles at 1K msg/s
- Solution: Use composite key (device_id#sequence_num) to spread across shards
  OR accept throttling (set retry with backoff)
```

#### Records
- **Sequence Number**: Unique ID per record within shard (auto-assigned, increasing)
- **Data blob**: Payload (JSON, binary, text), max **1MB**
- **Ordering**: Guaranteed within shard (by sequence number), NOT across shards

#### Retention
- **Default**: 24 hours
- **Extended**: Up to 365 days (configurable, extra cost)
- Records expire after retention period (auto-deleted)

#### Consumers

**Shared Fan-Out (Classic)**:
- **Throughput**: 2MB/sec per shard (shared across ALL consumers)
- 5 consumers reading from 1 shard → each gets ~400KB/sec
- **Latency**: ~200ms (consumers poll using `GetRecords`)
- **Use case**: Cost-sensitive, few consumers

**Enhanced Fan-Out (EFO)**:
- **Throughput**: 2MB/sec per consumer per shard (dedicated)
- 5 consumers → each gets full 2MB/sec
- **Latency**: ~70ms (push model using HTTP/2)
- **Cost**: $0.015 per shard-hour per consumer (more expensive)
- **Use case**: Low latency, many consumers

#### Capacity Modes

**Provisioned**:
- Specify shard count
- Pay per shard-hour
- Manual scaling (split/merge shards)

**On-Demand** (newer):
- Auto-scales based on throughput
- Pay per GB ingested/retrieved
- No shard management
- Max 200MB/sec write, 400MB/sec read (default, can increase)

#### Limits
- **Record size**: Max 1MB
- **Write per shard**: 1MB/sec or 1,000 records/sec
- **Read per shard**: 2MB/sec (shared) or 2MB/sec per consumer (EFO)
- **Retention**: Max 365 days
- **Enhanced fan-out consumers**: Max 20 per stream
- **PutRecords batch**: Max 500 records or 5MB

#### Error Handling
- **ProvisionedThroughputExceededException**: Exceeded shard capacity
  - Solution: Retry with exponential backoff, add shards, better partition key
- **KMS throttling**: Encryption/decryption calls exceed KMS limits
  - Solution: Request KMS limit increase, reduce write rate

#### Monitoring
- **CloudWatch Metrics**:
  - `IncomingBytes`, `IncomingRecords`: Write throughput
  - `GetRecords.Latency`: Consumer read latency
  - `WriteProvisionedThroughputExceeded`: Shard write throttling
  - `ReadProvisionedThroughputExceeded`: Shard read throttling
  - `IteratorAgeMilliseconds`: Age of last record read (lag indicator)

---

### Kinesis Data Firehose (ETL to Storage)

#### What It Does
- **Fully managed**: Load streaming data into destinations (no server management)
- **Destinations**: S3, Redshift, OpenSearch, Splunk, HTTP endpoints, Snowflake
- **Auto-scaling**: Handles throughput automatically (no shards to manage)
- **Transformation**: Optional Lambda transform before delivery
- **Batching**: Automatic batching/compression for cost optimization

#### Delivery Mechanism (Critical Interview Detail)

**Buffer Configuration** (Whichever Comes First):
```python
firehose.create_delivery_stream(
    DeliveryStreamName='logs-to-s3',
    S3DestinationConfiguration={
        'BucketARN': 'arn:aws:s3:::my-logs',
        'BufferingHints': {
            'SizeInMBs': 5,        # Buffer size: 1-128 MB
            'IntervalInSeconds': 300  # Buffer interval: 60-900 seconds
        },
        'CompressionFormat': 'GZIP',  # Compress before delivery
        'Prefix': 'logs/year=!{timestamp:yyyy}/month=!{timestamp:MM}/'
    }
)

# Delivery triggers when EITHER condition met:
# - Buffer size reaches 5MB
# - 300 seconds (5 minutes) elapsed
# Whichever happens FIRST

# Example timeline:
# t=0:     Start buffering
# t=30s:   Accumulated 1MB (< 5MB, < 300s) → continue buffering
# t=60s:   Accumulated 2MB → continue
# t=120s:  Accumulated 5MB → DELIVER (size threshold met)
# t=120s:  Reset buffer, start new batch

# Or:
# t=0:     Start buffering (low traffic)
# t=100s:  Only 500KB accumulated
# t=200s:  Still only 1MB
# t=300s:  Only 2MB, but 300s elapsed → DELIVER (time threshold met)
```

**Latency Implications**:
```python
# Minimum latency: 60 seconds (minimum buffer interval)
# Maximum latency: 900 seconds (maximum buffer interval)

# Scenario 1: High throughput (1MB/sec)
# Buffer: 5MB, 300s
# Latency: 5 seconds (5MB accumulated in 5s → delivered)

# Scenario 2: Low throughput (10KB/sec)
# Buffer: 5MB, 300s
# Latency: 300 seconds (5MB would take 500s, so 300s triggers first)

# ❌ Wrong expectation: Real-time delivery
# Firehose is NEAR real-time (60s - 900s latency)

# ✅ Use case: Batch analytics, data lake ingestion
# ❌ NOT for: Real-time dashboards, alerting (use Kinesis Data Streams + Lambda)
```

**Cost Optimization via Buffering**:
```python
# S3 pricing:
# - PUT requests: $0.005 per 1000 requests
# - Storage: $0.023 per GB

# Scenario: 1GB/hour streaming data

# Without Firehose (direct S3 PUTs):
# - 1 record/sec × 3600 sec = 3600 records/hour
# - 3600 PUT requests × $0.005/1000 = $0.018/hour
# - Per month: $0.018 × 730 = $13.14 (just PUT costs!)

# With Firehose (5MB buffer, 300s interval):
# - Batches: 1GB / 5MB = 200 batches/hour
# - 200 PUT requests × $0.005/1000 = $0.001/hour
# - Per month: $0.001 × 730 = $0.73 (94% reduction in PUT costs!)
# - Firehose cost: $0.029 per GB = $21.17/month (1GB/hour × 730 hours)
# - Total: $21.90/month vs $13.14 (more expensive, but managed service)

# Benefit: No code, auto-scaling, compression, partitioning
```

**Compression** (Huge Storage Savings):
```python
# Compression formats: GZIP, Snappy, Zip, Hadoop-compatible Snappy

# JSON logs: ~10:1 compression ratio with GZIP

# Without compression:
# 1GB/hour uncompressed × 730 hours = 730 GB/month
# Cost: 730 × $0.023 = $16.79/month (storage only)

# With GZIP compression:
# 1GB/hour compressed to 100MB × 730 hours = 73 GB/month
# Cost: 73 × $0.023 = $1.68/month (90% savings!)

# Tradeoff: CPU cost to decompress when querying (Athena, Spark)
# But: Athena charges per data scanned
# - Query 730GB uncompressed: $3.65 per full scan
# - Query 73GB compressed: $0.365 per full scan (90% savings!)
```

**Dynamic Partitioning** (S3 Organization):
```python
# Problem: All data in one S3 prefix → slow queries

# Without partitioning:
# s3://logs/data.gz (all data in one file or prefix)
# Athena query for specific date: Scans ALL data ($$$)

# ✅ With dynamic partitioning:
firehose.create_delivery_stream(
    DeliveryStreamName='partitioned-logs',
    ExtendedS3DestinationConfiguration={
        'BucketARN': 'arn:aws:s3:::my-logs',
        'DynamicPartitioningConfiguration': {
            'Enabled': True
        },
        'ProcessingConfiguration': {
            'Enabled': True,
            'Processors': [{
                'Type': 'MetadataExtraction',
                'Parameters': [{
                    'ParameterName': 'JsonParsingEngine',
                    'ParameterValue': 'JQ-1.6'
                }, {
                    'ParameterName': 'MetadataExtractionQuery',
                    'ParameterValue': '{year:.timestamp[0:4], month:.timestamp[5:7], day:.timestamp[8:10]}'
                }]
            }]
        },
        'Prefix': 'logs/year=!{partitionKeyFromQuery:year}/month=!{partitionKeyFromQuery:month}/day=!{partitionKeyFromQuery:day}/',
        'ErrorOutputPrefix': 'errors/'
    }
)

# Input record:
# {"timestamp": "2024-01-15T10:30:00", "level": "ERROR", "msg": "..."}

# Output S3 path:
# s3://my-logs/logs/year=2024/month=01/day=15/data-xxx.gz

# Query benefits:
# Query for 2024-01-15: Scans only that day's partition
# Cost: 1/365th of full table scan (99.7% savings!)
```

**Lambda Transformation** (Data Enrichment):
```python
# Transform records before delivery

# Lambda function:
def lambda_handler(event, context):
    output = []

    for record in event['records']:
        # Decode base64 data
        payload = base64.b64decode(record['data'])
        data = json.loads(payload)

        # Transform: Add processed timestamp, enrich data
        data['processed_at'] = datetime.utcnow().isoformat()
        data['enriched_field'] = lookup_database(data['user_id'])

        # Remove PII
        data.pop('email', None)
        data.pop('ssn', None)

        # Encode back to base64
        output_data = base64.b64encode(
            json.dumps(data).encode('utf-8')
        ).decode('utf-8')

        output.append({
            'recordId': record['recordId'],
            'result': 'Ok',  # or 'Dropped' to skip, or 'ProcessingFailed'
            'data': output_data
        })

    return {'records': output}

# Firehose behavior:
# - Batches records to Lambda (up to 3MB or 500 records)
# - Lambda processes batch
# - Firehose retries failures (exponential backoff)
# - Failed records after retries → error bucket

# Cost:
# - Lambda: $0.20 per 1M requests + compute duration
# - 1GB/hour @ 1KB/record = 1M records/hour
# - Lambda cost: ~$5-10/month (depending on transform complexity)
```

**Firehose vs Kinesis Data Streams**:

| Feature | Data Streams | Firehose |
|---------|--------------|----------|
| **Latency** | 70-200ms (real-time) | 60-900s (near real-time) |
| **Management** | Manual (shards) | Fully managed |
| **Consumers** | Custom (KCL, Lambda) | Fixed destinations |
| **Retention** | 24h-365d (replay) | None (delivers immediately) |
| **Cost** | $0.015/shard-hour | $0.029/GB ingested |
| **Transformation** | Consumer logic | Built-in Lambda |
| **Use case** | Real-time processing | Data lake ingestion |

**When to use Firehose**:
- Batch delivery to S3/Redshift/OpenSearch acceptable (60s+ latency)
- Don't need custom consumers (just load to destination)
- Want managed service (no shard management)
- Example: Log aggregation, analytics data lake, archival

**When to use Data Streams**:
- Need real-time processing (< 1 second)
- Multiple custom consumers
- Need replay capability
- Example: Fraud detection, real-time dashboards, event-driven architectures

#### Features
- **Compression**: GZIP, Snappy, Zip (for S3)
- **Encryption**: At rest (S3, Redshift) and in transit
- **Batching**: Automatically batches records
- **Retry**: Auto-retry failed deliveries
- **Backup**: Can send all/failed records to S3 backup bucket

#### Data Firehose vs Data Streams

| Feature | Data Streams | Firehose |
|---------|--------------|----------|
| **Latency** | Real-time (~200ms) | Near real-time (60s min buffer) |
| **Scaling** | Manual (shards) | Automatic |
| **Storage** | 24h-365d retention | No storage (delivers to destination) |
| **Consumers** | Custom code (KCL, Lambda) | Pre-defined destinations |
| **Replay** | Yes (within retention) | No |
| **Cost** | Per shard-hour | Per GB ingested |

**When to use**:
- **Streams**: Real-time processing, custom consumers, need replay
- **Firehose**: Simple ETL, load into S3/Redshift, no custom processing

---

### Kinesis Data Analytics

- **SQL queries** on streaming data (Streams or Firehose)
- **Use cases**: Real-time dashboards, metrics, alerts, aggregations
- **Output**: Streams, Firehose, Lambda
- **Auto-scaling**: Handles throughput automatically
- Supports **Apache Flink** for advanced Java/Scala processing

---

### Best Practices
1. **Partition key**: Use high-cardinality key (avoid hot shards)
2. **Batch puts**: Use `PutRecords` (batch up to 500) instead of `PutRecord` (single)
3. **Monitoring**: Watch `IteratorAgeMilliseconds` (consumer lag)
4. **Error handling**: Exponential backoff for throttling
5. **Enhanced fan-out**: Use for low-latency, multiple consumers
6. **Firehose for ETL**: If just loading to S3/Redshift, use Firehose (simpler)
7. **Shard count**: Over-provision slightly (cheaper to have extra shards than retry logic)

---

## SQS (Simple Queue Service)

### Core Concepts
- **Fully managed message queue**: Decouple producers and consumers
- **Unlimited throughput**: Unlimited messages/sec (no provisioning needed)
- **Unlimited messages**: No limit on queue size (scales infinitely)
- **Retention**: 1 minute to 14 days (default 4 days)
- **Message size**: Max **256KB** (use S3 for larger, send pointer)
- **At-least-once delivery** (Standard) or **Exactly-once** (FIFO)

### SQS Hard Limits & Critical Constraints

| Limit | Standard Queue | FIFO Queue | Impact |
|-------|----------------|------------|--------|
| **Message size** | 256KB | 256KB | Larger msgs fail, use S3 pointer |
| **Message retention** | 1 min - 14 days | 1 min - 14 days | Messages expire after retention |
| **Throughput** | Unlimited | 300 msg/s (3000 with batching) | FIFO throttles at limit |
| **Visibility timeout** | 0 sec - 12 hours | 0 sec - 12 hours | Max time to process before redelivery |
| **In-flight messages** | 120,000 | 20,000 | New receives blocked when exceeded |
| **Batch size** | 10 messages | 10 messages | Multiple API calls needed for >10 |
| **Message attributes** | 10 per message | 10 per message | Cannot add more |
| **Delay** | 0 - 15 minutes | 0 - 15 minutes | Cannot delay >15 min |
| **Long poll wait** | 0 - 20 seconds | 0 - 20 seconds | Max wait for messages |
| **Message deduplication** | No | 5-minute window | FIFO only |
| **Queue name** | 80 chars | 80 chars (must end .fifo) | Naming fails otherwise |

**Critical Interview Edge Cases**:

#### 1. **256KB Message Size Limit** (Very Common Pattern):
```python
# ❌ Problem: Need to send 5MB payload
message = {
    'orderId': '12345',
    'customerData': '<5MB of data>',
    'invoice': '<PDF bytes>'
}
sqs.send_message(MessageBody=json.dumps(message))
# Error: MessageTooLong (max 256KB)

# ✅ Solution: S3 Pointer Pattern
# Step 1: Upload large payload to S3
s3_key = f"orders/{order_id}/payload.json"
s3.put_object(Bucket='my-bucket', Key=s3_key, Body=large_payload)

# Step 2: Send pointer via SQS
message = {
    'orderId': '12345',
    's3Bucket': 'my-bucket',
    's3Key': s3_key,
    'messageType': 'order_created'
}
sqs.send_message(MessageBody=json.dumps(message))  # < 1KB

# Step 3: Consumer retrieves from S3
msg = sqs.receive_message()
payload = s3.get_object(
    Bucket=msg['s3Bucket'],
    Key=msg['s3Key']
)

# Step 4: Cleanup (optional)
sqs.delete_message(ReceiptHandle=msg['ReceiptHandle'])
s3.delete_object(Bucket=msg['s3Bucket'], Key=msg['s3Key'])

# AWS SDK Extended Client Library automates this pattern
```

#### 2. **In-Flight Messages Limit** (120K Standard, 20K FIFO):
```python
# In-flight = received but not deleted (visibility timeout active)

# Scenario: 200K consumers, each polls 1 message
# Standard queue: First 120K succeed, next 80K get empty response
# Error: No error, just no messages returned (soft limit)

# FIFO queue: First 20K succeed, next 180K get empty
# Much more restrictive!

# ✅ Solution:
# 1. Process faster (delete messages quickly)
# 2. Reduce visibility timeout (return to queue faster if not processed)
# 3. Use multiple queues (shard across queues)
```

#### 3. **Visibility Timeout** (Most Misunderstood Feature):
```python
# When message received → becomes invisible for timeout duration

# t=0: Consumer A receives message
#      Message invisible for 30 seconds (default)
# t=10: Consumer A still processing
# t=30: Consumer A crashes (didn't delete message)
# t=30: Message visible again
# t=31: Consumer B receives same message (redelivery)

# ❌ Problem: Timeout too short
# Visibility: 30 sec, processing takes 60 sec
# → Message redelivered while still processing (duplicate processing)

# ✅ Solution 1: Set timeout > max processing time
ReceiveMessage(VisibilityTimeout=300)  # 5 minutes

# ✅ Solution 2: Extend timeout during processing
sqs.change_message_visibility(
    ReceiptHandle=receipt,
    VisibilityTimeout=300  # Extend by 5 more minutes
)

# ✅ Solution 3: Heartbeat pattern
def process_with_heartbeat(message):
    receipt = message['ReceiptHandle']

    for chunk in process_in_chunks(message):
        process_chunk(chunk)
        # Extend timeout every minute
        sqs.change_message_visibility(
            ReceiptHandle=receipt,
            VisibilityTimeout=300
        )
```

### Queue Types (Critical Design Decision)

#### Standard Queue
- **Ordering**: Best-effort, **NOT guaranteed** (can be out of order)
- **Delivery**: **At-least-once** (duplicates possible, common!)
- **Throughput**: Unlimited (millions of messages/sec)
- **Latency**: Sub-millisecond (very low)
- **Use case**: High throughput, ordering not critical, idempotent consumers

**At-Least-Once Delivery Example**:
```python
# Send message once
sqs.send_message(MessageBody='charge customer $100')

# Consumer may receive message TWICE:
# Receive 1: Process, charge $100
# Receive 2: (Due to distributed nature) charge $100 again!

# ✅ Must be idempotent:
def process_message(msg):
    order_id = msg['orderId']

    # Check if already processed (DDB, Redis, etc.)
    if already_processed(order_id):
        return  # Skip duplicate

    charge_customer(100)
    mark_processed(order_id)  # Store in DDB
```

#### FIFO Queue
- **Ordering**: **Strict order** within message group (guaranteed)
- **Delivery**: **Exactly-once** processing (no duplicates in 5-min window)
- **Throughput**:
  - **Without batching**: 300 transactions/sec
  - **With batching** (10 msgs/batch): 3,000 messages/sec
  - **High-throughput mode**: 30,000 msg/sec (9,000 transactions/sec with batching)
- **Naming**: Queue name must end with `.fifo` (e.g., `orders.fifo`)
- **Message Group ID**: All messages with same group ID are ordered
  - Different groups can be processed in parallel
- **Deduplication**: 5-minute deduplication window
  - **Content-based**: SHA-256 hash of message body
  - **Deduplication ID**: Explicit token (more control)

**How FIFO Ordering Actually Works — Sequence Numbers & Group Locking**:
```
Write side — SQS assigns a sequence number per message group:

  Producer sends for group "user_123":
    payment_1 → arrives first  → sequence: 1
    payment_2 → arrives second → sequence: 2
    payment_3 → arrives third  → sequence: 3

  Messages are appended to an ordered log per group. Sequence number
  is monotonically increasing and assigned by SQS (not the producer).

Read side — Only ONE message per group is visible at a time:

  Consumer polls → gets payment_1 (seq 1)
    → SQS LOCKS entire group "user_123"
    → No consumer can see payment_2 until payment_1 is DELETED

  Consumer deletes payment_1 → group unlocked
    → Next poll returns payment_2 (seq 2)

  Consumer deletes payment_2 → group unlocked
    → Next poll returns payment_3 (seq 3)

This is why FIFO is slow — processing is SEQUENTIAL within a group.
Parallelism only happens ACROSS different groups:

  group "user_123": payment_1 → payment_2 → payment_3  (sequential)
  group "user_456": payment_1 → payment_2              (sequential)
  group "user_789": payment_1                           (sequential)
       ↑ these three groups process in PARALLEL

Key implication: If consumer crashes without deleting the message,
the visibility timeout expires, the SAME message becomes visible again,
and the group stays blocked until that message is successfully processed
and deleted. The group cannot move forward.
```

**FIFO Message Group Parallelism**:
```python
# Scenario: Order processing system

# ❌ Problem: Single message group → no parallelism
sqs.send_message(
    MessageBody='order_data',
    MessageGroupId='all_orders',  # BAD: All messages in one group
    MessageDeduplicationId='order_12345'
)
# Result: 300 msg/s throughput (sequential processing)

# ✅ Solution: Multiple message groups (parallel processing)
sqs.send_message(
    MessageBody='order_data',
    MessageGroupId=f'user_{user_id}',  # Group by user
    MessageDeduplicationId='order_12345'
)

# With 1000 users:
# - Each user's orders processed in order (within group)
# - Different users processed in parallel
# - Throughput: 300 msg/s × num_consumers (up to limit)

# Rule: Use high-cardinality group IDs (userIds, not status)
```

**Deduplication Deep Dive**:
```python
# Method 1: Content-based (automatic)
sqs.send_message(
    MessageBody='{"orderId": "12345", "amount": 100}',
    MessageGroupId='user_123'
    # No dedup ID → SHA-256 hash of body used
)

# Send again within 5 minutes (same body):
sqs.send_message(
    MessageBody='{"orderId": "12345", "amount": 100}',  # Identical
    MessageGroupId='user_123'
)
# Result: Second send ignored (duplicate detected)

# Method 2: Explicit deduplication ID (recommended)
import uuid
sqs.send_message(
    MessageBody=json.dumps({
        'orderId': '12345',
        'amount': 100,
        'timestamp': time.time()  # Body changes each time
    }),
    MessageGroupId='user_123',
    MessageDeduplicationId='order_12345'  # Explicit (best practice)
)

# Retry with same dedup ID (network failure retry):
sqs.send_message(
    MessageBody=json.dumps({...}),  # Body might differ (timestamp)
    MessageGroupId='user_123',
    MessageDeduplicationId='order_12345'  # Same ID
)
# Result: Safely ignored (already sent)

# After 5 minutes: Same dedup ID allowed (new message)
```

**FIFO Throughput Limits** (Interview Question):
```
Q: Need 50,000 msg/sec with ordering. FIFO queue?

A: ❌ No. FIFO max = 30,000 msg/sec (high-throughput mode)

Solutions:
1. ✅ Standard queue + application-level ordering (complex)
2. ✅ Shard across multiple FIFO queues:
   - 10 FIFO queues × 3,000 msg/s = 30,000 msg/s
   - Route by hash(key) % 10
3. ✅ Kinesis Data Streams (natural fit for high-throughput ordered data)
```

### Message Lifecycle

```
Producer → SQS Queue → Consumer polls → Process → Delete
                ↓
         (Visibility Timeout)
                ↓
         If not deleted → visible again (redelivery)
```

#### Visibility Timeout
- **Default**: 30 seconds
- **Max**: 12 hours
- When consumer receives message, it becomes **invisible** to other consumers
- If not deleted within timeout → becomes visible again (redelivery)
- **ChangeMessageVisibility**: Extend timeout if processing takes longer
- **Best practice**: Set to 6× average processing time

#### Message Retention
- **Default**: 4 days
- **Range**: 1 minute to 14 days
- After retention expires → message deleted from queue

### Polling

#### Short Polling (Default)
- Returns immediately (even if queue empty)
- May not check all servers (subset)
- **More API calls** → higher cost
- **ReceiveMessageWaitTimeSeconds = 0**

#### Long Polling (Recommended)
- Waits up to 20 seconds for messages
- Checks all servers
- **Fewer API calls** → lower cost, reduced latency
- **ReceiveMessageWaitTimeSeconds = 1-20**
- **Best practice**: Always use long polling

### Dead Letter Queue (DLQ) - Critical for Production

**Core Concept**:
- Queue for messages that fail processing repeatedly
- **Max Receives**: After X failed attempts → send to DLQ
- **Use case**: Debug failed messages, alert on failures, prevent poison pills
- **Important**: DLQ must be same type (Standard/FIFO) as source
- **Retention**: Set longer retention on DLQ (e.g., 14 days)

**Detailed Mechanics** (Interview Critical):

```python
# Setup: Main queue with DLQ
main_queue = sqs.create_queue(QueueName='orders-queue')
dlq = sqs.create_queue(QueueName='orders-dlq')

# Configure DLQ on main queue
sqs.set_queue_attributes(
    QueueUrl=main_queue['QueueUrl'],
    Attributes={
        'RedrivePolicy': json.dumps({
            'deadLetterTargetArn': dlq_arn,
            'maxReceiveCount': '3'  # Move to DLQ after 3 failed attempts
        })
    }
)

# What counts as a "receive"?
# - Each time message becomes visible (after visibility timeout expires)
# - NOT the initial receive

# Timeline of a failing message:
# t=0:   Consumer A receives message (ReceiveCount = 1)
#        Visibility timeout = 30s
# t=10:  Consumer A crashes (didn't delete message)
# t=30:  Message visible again
# t=31:  Consumer B receives message (ReceiveCount = 2)
# t=40:  Consumer B crashes
# t=61:  Message visible again
# t=62:  Consumer C receives message (ReceiveCount = 3)
# t=70:  Consumer C crashes
# t=92:  Message MOVED to DLQ (maxReceiveCount=3 exceeded)
#        Message no longer in main queue
```

**Critical Edge Cases**:

1. **Receive Count Reset** (Common Misunderstanding):
```python
# ❌ Wrong assumption: Delete then re-send resets receive count
message = sqs.receive_message(QueueUrl=queue_url)
# ReceiveCount: 2

# Delete message
sqs.delete_message(ReceiptHandle=message['ReceiptHandle'])

# Re-send to same queue
sqs.send_message(QueueUrl=queue_url, MessageBody=same_body)

# New message ReceiveCount: 0 (it's a NEW message with new MessageId)
# Original message gone, receive count NOT transferred

# ✅ Correct: Receive count is per MESSAGE ID, not content
# Each unique MessageId has its own receive count
```

2. **DLQ Redrive** (Moving Messages Back):
```python
# Messages in DLQ can be moved back to source queue

# Method 1: Manual redrive (Console or API)
sqs.start_message_move_task(
    SourceArn=dlq_arn,
    DestinationArn=main_queue_arn,
    MaxNumberOfMessagesPerSecond=10  # Throttle rate
)

# Method 2: Process from DLQ, fix issue, send to main queue
dlq_messages = sqs.receive_message(
    QueueUrl=dlq_url,
    MaxNumberOfMessages=10
)

for msg in dlq_messages['Messages']:
    # Fix the message (e.g., add missing field)
    fixed_body = fix_message(msg['Body'])

    # Send to main queue
    sqs.send_message(
        QueueUrl=main_queue_url,
        MessageBody=fixed_body
    )

    # Delete from DLQ
    sqs.delete_message(
        QueueUrl=dlq_url,
        ReceiptHandle=msg['ReceiptHandle']
    )

# Method 3: Automated redrive with Lambda
# Lambda triggered by CloudWatch alarm (DLQ not empty)
# Attempts to process DLQ messages with different logic
```

3. **DLQ Monitoring** (Production Critical):
```python
# CloudWatch metrics:
# - ApproximateNumberOfMessagesVisible (DLQ)
# - NumberOfMessagesSent (DLQ)

# ✅ Best practice: Alert when DLQ receives messages
cloudwatch.put_metric_alarm(
    AlarmName='orders-dlq-not-empty',
    MetricName='ApproximateNumberOfMessagesVisible',
    Namespace='AWS/SQS',
    Dimensions=[{'Name': 'QueueName', 'Value': 'orders-dlq'}],
    Statistic='Average',
    Period=60,
    EvaluationPeriods=1,
    Threshold=1,  # Alert if even 1 message in DLQ
    ComparisonOperator='GreaterThanOrEqualToThreshold',
    AlarmActions=[sns_topic_arn]  # Page on-call engineer
)

# Why this matters:
# - DLQ messages = business logic failures
# - Could be: payment processing, order fulfillment, critical workflows
# - Manual intervention often needed (not auto-retry)
```

4. **Poison Pill Messages**:
```python
# Poison pill = message that always fails processing

# Example: Malformed JSON
message_body = "This is not JSON {{{"

# Consumer code:
def process_message(msg):
    data = json.loads(msg['Body'])  # ← Fails every time!
    process_order(data)

# What happens:
# 1. Consumer receives → fails to parse → doesn't delete
# 2. After visibility timeout → visible again
# 3. Another consumer receives → fails to parse
# 4. Repeats maxReceiveCount times (e.g., 3)
# 5. Moved to DLQ ✅ (prevents infinite retry loop)

# Without DLQ:
# - Message retried forever
# - Blocks other messages (if sequential processing)
# - Wastes compute (consumers constantly failing)

# ✅ Best practice: Defensive parsing
def process_message(msg):
    try:
        data = json.loads(msg['Body'])
    except json.JSONDecodeError:
        # Log the error
        logger.error(f"Invalid JSON: {msg['Body']}")
        # Delete message (don't retry)
        sqs.delete_message(ReceiptHandle=msg['ReceiptHandle'])
        return

    process_order(data)
```

5. **DLQ for FIFO Queues** (Special Behavior):
```python
# FIFO queue + DLQ = preserves message group ordering

# Main FIFO queue: orders.fifo
# DLQ: orders-dlq.fifo (must also be FIFO!)

# Scenario:
# Message group: user_123
# - Message 1: Process ✅ (deleted)
# - Message 2: Fails 3× → DLQ
# - Message 3: BLOCKED (waiting for message 2)

# After message 2 moved to DLQ:
# - Message 3: Now processed ✅
# - Message 4: Processed ✅

# Key insight: DLQ unblocks message group
# Otherwise message 3+ would wait forever

# DLQ messages retain:
# - MessageGroupId
# - MessageDeduplicationId
# - Original sequence

# Redrive from FIFO DLQ:
# - Messages moved back in original order
# - Message group ordering maintained
```

6. **Cost Implications**:
```python
# Scenario: 1M messages/day, 5% fail 3 times before DLQ

# Without DLQ:
# - 50K failed messages retry forever
# - 50K × 3 retries × 3 attempts = 450K extra receives/day
# - Cost: 450K requests (wasted compute, delayed processing)

# With DLQ:
# - 50K messages moved to DLQ after 3 attempts
# - 50K × 3 attempts = 150K receives (then DLQ)
# - Savings: 300K fewer retries
# - Benefit: Failed messages isolated, can debug offline

# DLQ retention cost:
# - DLQ retention: 14 days (vs 4 days main queue)
# - Cost: $0 (retention is free, only pay for requests)
```

**Interview Question**: *"Queue processes 10K messages/hour. 1% fail processing due to downstream service outage. How do you prevent failed messages from blocking the queue?"*

**Answer**:
```
Set up a Dead Letter Queue with maxReceiveCount=3:

1. Failed messages retry 3 times (with exponential backoff)
2. After 3 failures → moved to DLQ
3. Main queue continues processing successfully (not blocked by failures)
4. DLQ triggers alarm → alerts ops team
5. Once downstream service recovers:
   - Fix root cause
   - Redrive messages from DLQ to main queue
   - Messages reprocessed successfully

Benefits:
- Prevents poison pills from blocking queue
- Isolates failures for debugging
- Allows continued processing of healthy messages
- Provides visibility into failure patterns (CloudWatch metrics on DLQ)

Alternative: If failure is transient (network timeout), increase visibility timeout and retry count before DLQ.
```

### Message Attributes
- **System attributes**: Sent automatically (timestamp, sender ID, etc.)
- **Custom attributes**: Key-value metadata (up to 10 attributes)
- **Message deduplication ID** (FIFO): For deduplication
- **Message group ID** (FIFO): For ordering within group

### Delay Queues
- **Delay seconds**: Delay message delivery (0-900 seconds, max 15 min)
- **Queue-level**: All messages delayed
- **Message-level**: Individual message delay (overrides queue setting)
- **Use case**: Delayed job processing, rate limiting

### Batching
- **SendMessageBatch**: Send up to 10 messages in one API call
- **ReceiveMessage**: Receive up to 10 messages at once
- **DeleteMessageBatch**: Delete up to 10 messages
- **Benefits**: Fewer API calls, lower cost, higher throughput

### Limits
- **Message size**: 256KB (use Extended Client Library for S3 pointer pattern)
- **In-flight messages**: 120,000 (Standard), 20,000 (FIFO)
- **Batch size**: 10 messages
- **Visibility timeout**: 12 hours max
- **Message retention**: 14 days max
- **Queue name**: 80 chars, alphanumeric + hyphen + underscore

### Security
- **Encryption at rest**: SSE-SQS (AWS managed) or SSE-KMS (customer managed)
- **Encryption in transit**: HTTPS (TLS)
- **Access control**: IAM policies, SQS access policies (resource-based)
- **SQS access policy**: Grant cross-account access, allow S3/SNS to send messages

### Monitoring
- **CloudWatch Metrics**:
  - `ApproximateNumberOfMessagesVisible`: Messages in queue
  - `ApproximateNumberOfMessagesNotVisible`: In-flight (being processed)
  - `ApproximateAgeOfOldestMessage`: Age of oldest message (backlog indicator)
  - `NumberOfMessagesSent`, `NumberOfMessagesReceived`, `NumberOfMessagesDeleted`

### Common Patterns

#### Fan-Out: SNS → SQS
- SNS topic → multiple SQS queues (subscribers)
- Each queue gets copy of message
- Parallel processing, different consumers

#### Priority Queue
- Use **two queues** (high-priority, low-priority)
- Consumer checks high-priority first, then low-priority

#### Request-Response
- Producer sends message with `ReplyTo` queue name
- Consumer processes, sends response to reply queue
- Use **correlation ID** to match request/response

### Best Practices
1. **Use long polling**: Reduce cost, latency
2. **Set visibility timeout**: 6× average processing time
3. **Use DLQ**: Capture failed messages for debugging
4. **Batch operations**: SendMessageBatch, DeleteMessageBatch
5. **Idempotent processing**: Handle duplicate deliveries (Standard queue)
6. **Delete messages**: Always delete after successful processing (prevent redelivery)
7. **Monitor age**: Alert on `ApproximateAgeOfOldestMessage` (backlog)
8. **FIFO only when needed**: Use Standard for higher throughput if order not critical

---

## SNS (Simple Notification Service)

### Core Concepts
- **Pub/Sub messaging**: Publishers send to topics, subscribers receive (1-to-many)
- **Fan-out**: One message → many subscribers (up to 12.5M per topic)
- **Push-based**: SNS pushes to subscribers (unlike SQS pull model)
- **Protocols**: HTTP/HTTPS, Email, SMS, SQS, Lambda, Mobile push, Kinesis Firehose
- **Durability**: Messages stored redundantly across multiple AZs
- **Throughput**: Unlimited (scales automatically)

### SNS Hard Limits & Constraints

| Limit | Value | Impact | Workaround |
|-------|-------|--------|------------|
| **Message size** | 256KB | Larger messages fail | Use S3 pointer pattern |
| **Subscribers per topic** | 12.5M | Cannot add more | Use multiple topics |
| **Topics per account** | 100,000 | Soft limit | Request increase |
| **Filter policies** | 200 conditions | Complex filters fail | Simplify or use Lambda |
| **Message attributes** | 10 per message | Cannot add more | Embed in message body |
| **SMS rate** | 1 msg/sec (default) | Throttles SMS sends | Request increase |
| **Delivery retries** | Varies by protocol | Failed deliveries dropped after retries | Use DLQ |
| **Message retention** | None (fire and forget) | No built-in replay | Use SQS for durability |
| **FIFO throughput** | 300 msg/s (3000 batched) | Same as SQS FIFO | Use Standard for higher |

**Critical Pattern: SNS → SQS Fan-Out** (Most Common Use Case):
```python
# Problem: Need to send one event to multiple systems
# - Thumbnail generation service
# - Metadata extraction service
# - Virus scanning service

# ❌ Bad: Direct fan-out from app
# App sends to 3 SQS queues (tight coupling, what if add 4th system?)

# ✅ Good: SNS topic → multiple SQS subscribers
# Step 1: Create SNS topic
topic = sns.create_topic(Name='image-uploaded')

# Step 2: Create SQS queues and subscribe to topic
queues = ['thumbnail-queue', 'metadata-queue', 'virus-queue']
for queue_name in queues:
    queue = sqs.create_queue(QueueName=queue_name)
    sns.subscribe(
        TopicArn=topic['TopicArn'],
        Protocol='sqs',
        Endpoint=queue['QueueArn']
    )

    # Grant SNS permission to send to SQS
    sqs.set_queue_attributes(
        QueueUrl=queue['QueueUrl'],
        Attributes={
            'Policy': json.dumps({
                'Statement': [{
                    'Effect': 'Allow',
                    'Principal': {'Service': 'sns.amazonaws.com'},
                    'Action': 'sqs:SendMessage',
                    'Resource': queue['QueueArn'],
                    'Condition': {
                        'ArnEquals': {'aws:SourceArn': topic['TopicArn']}
                    }
                }]
            })
        }
    )

# Step 3: Publish once, all subscribers receive
sns.publish(
    TopicArn=topic['TopicArn'],
    Message=json.dumps({
        's3Bucket': 'images',
        's3Key': 'uploads/photo.jpg',
        'userId': 'user_123'
    })
)

# All 3 queues receive the message (independent processing)
# Add 4th subscriber later: No app code changes!
```

### Topic Types

#### Standard Topic
- **Ordering**: Best-effort, not guaranteed
- **Throughput**: Unlimited
- **Use case**: Most scenarios

#### FIFO Topic
- **Ordering**: Strict order (within message group)
- **Deduplication**: Exactly-once delivery (5-min window)
- **Subscribers**: Only SQS FIFO queues
- **Throughput**: Same as SQS FIFO (300 msg/sec, 3,000 with batching)
- **Use case**: Need ordering + fan-out to multiple SQS queues

### Publishing

#### Message Structure
```json
{
  "Message": "The actual message body",
  "Subject": "Optional subject (for email)",
  "MessageAttributes": {
    "key1": { "DataType": "String", "StringValue": "value1" }
  }
}
```

#### Message Filtering
- Subscribers can set **filter policies** (JSON)
- Only receive messages matching filter
- **Reduces**: Unnecessary deliveries, processing, cost
- **Max**: 200 conditions per policy

```json
{
  "store": ["example_corp"],
  "price": [{"numeric": [">=", 100]}]
}
```

### Subscriber Protocols

| Protocol | Use Case | Notes |
|----------|----------|-------|
| **SQS** | Decouple, buffer | Most common, enables fan-out |
| **Lambda** | Serverless processing | Auto-invokes function |
| **HTTP/HTTPS** | Webhooks, external systems | Must confirm subscription |
| **Email** | Alerts, notifications | JSON or plain text |
| **SMS** | Mobile alerts | Limited regions, rate limits |
| **Mobile Push** | iOS/Android apps | Via APNS, FCM, etc. |
| **Kinesis Firehose** | Stream to S3, Redshift | Near real-time delivery |

### Delivery Policies
- **Retry**: Auto-retry failed deliveries (HTTP/HTTPS)
  - Immediate retry (no delay)
  - Post-retry backoff phase (exponential)
  - Pre-backoff phase (no delay)
- **Throttling**: Max delivery rate
- **Dead Letter Queue**: Failed messages → SQS DLQ

### Message Attributes
- **Key-value metadata** (up to 10 attributes)
- **Data types**: String, Number, Binary, String.Array
- **Use case**: Filtering, routing, metadata

### Raw Message Delivery
- **Default**: SNS wraps message in JSON envelope
- **Raw mode**: Delivers original message (no wrapping)
- **Use case**: SQS, HTTP/HTTPS subscribers expecting raw format

### Security
- **Encryption at rest**: KMS (optional)
- **Encryption in transit**: HTTPS (TLS)
- **Access control**: IAM policies, SNS topic policies
- **Topic policy**: Grant cross-account access, allow S3/CloudWatch to publish

### Limits
- **Subscribers**: 12.5M per topic
- **Topic name**: 256 chars
- **Message size**: 256KB
- **Message attributes**: 10 per message
- **Filter policies**: 200 conditions

### Monitoring
- **CloudWatch Metrics**:
  - `NumberOfMessagesPublished`: Messages sent to topic
  - `NumberOfNotificationsDelivered`: Successful deliveries
  - `NumberOfNotificationsFailed`: Failed deliveries
- **Delivery status logging**: Track delivery to Lambda, SQS, HTTP, etc.

### Common Patterns

#### Fan-Out (SNS → SQS)
- **Problem**: Send one message to multiple systems
- **Solution**: Publish to SNS topic → multiple SQS queues subscribe
- **Benefits**: Parallel processing, decouple systems, different processing logic

```
S3 Event → SNS Topic → SQS Queue 1 (thumbnail service)
                    → SQS Queue 2 (metadata extraction)
                    → SQS Queue 3 (virus scan)
```

#### Application Alerts
- CloudWatch Alarm → SNS Topic → Email/SMS/Slack (HTTP endpoint)

#### Event Notifications
- S3, DynamoDB, etc. → SNS → multiple consumers

### Best Practices
1. **Use SQS for buffering**: SNS → SQS (durability, retry, backpressure)
2. **Message filtering**: Reduce unnecessary deliveries
3. **Idempotent processing**: Handle duplicate deliveries
4. **DLQ for HTTP/HTTPS**: Capture failed webhooks
5. **Monitor failures**: Alert on `NumberOfNotificationsFailed`
6. **Raw delivery**: Enable for SQS subscribers (avoid double JSON wrapping)
7. **FIFO only when needed**: Use Standard for higher throughput

---

## EC2 (Elastic Compute Cloud)

### Core Concepts
- **Virtual servers** in the cloud (VMs on AWS infrastructure)
- **Instance types**: 500+ types with different CPU, memory, storage, networking
- **AMI** (Amazon Machine Image): OS + software template (bootable image)
- **Elastic**: Scale up/down, start/stop on demand (pay only for running hours)

### EC2 Hard Limits & Key Constraints

| Limit | Value | Type | Impact | Solution |
|-------|-------|------|--------|----------|
| **On-Demand vCPU** | 1,280 vCPUs per region (default) | Soft | Cannot launch more instances | Request limit increase |
| **Spot vCPU** | Dynamic (based on availability) | Market | Spot request fails | Try different instance type/AZ |
| **EBS volumes** | 5,000 per region | Soft | Cannot create more | Request increase or delete unused |
| **EBS snapshots** | 100,000 per region | Soft | Backup limit | Archive to Glacier or delete old |
| **Security groups** | 500 per VPC | Soft | Cannot create more | Consolidate or request increase |
| **Rules per SG** | 60 inbound + 60 outbound | Soft | Complex SG fails | Consolidate CIDR blocks |
| **Elastic IPs** | 5 per region | Soft | IP exhaustion | Request increase or use NLB |
| **Instance metadata** | 16KB | Hard | Large metadata truncated | Store in S3, reference in user data |
| **Instance storage** | Varies by type (ephemeral) | Hard | Data lost on stop | Use EBS for persistence |
| **Network bandwidth** | Varies by instance type | Hard | Bottleneck at limit | Use larger instance or placement group |

**Critical Interview Topic: Instance Metadata Service (IMDS)**
```bash
# Every EC2 instance can query metadata about itself
# No authentication required (local-only endpoint)

# IMDSv1 (Legacy, less secure):
curl http://169.254.169.254/latest/meta-data/ami-id
# Returns: ami-12345678

# IMDSv2 (Recommended, session-based):
# Step 1: Get token
TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

# Step 2: Use token to query metadata
curl -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id
# Returns: i-1234567890abcdef

# Common metadata queries:
# - /meta-data/instance-id: Instance ID
# - /meta-data/public-ipv4: Public IP
# - /meta-data/iam/security-credentials/<role-name>: Temporary IAM credentials
# - /user-data: User data script

# Security consideration (SSRF vulnerability):
# If app makes HTTP requests based on user input:
# Attacker input: http://169.254.169.254/latest/meta-data/iam/security-credentials/MyRole
# → Leaks IAM credentials!

# ✅ Mitigation:
# 1. Use IMDSv2 (prevents simple SSRF)
# 2. Firewall rules to block 169.254.0.0/16 from app
# 3. Hop limit = 1 (only local process can access)

aws ec2 modify-instance-metadata-options \
  --instance-id i-1234567890 \
  --http-tokens required \  # IMDSv2 only
  --http-put-response-hop-limit 1  # No forwarding
```

**EBS vs Instance Store** (Critical for Interviews):
```python
# EBS (Elastic Block Store) - Network-attached
# - Persistent: Data survives stop/start/termination (if configured)
# - Performance: Up to 64,000 IOPS (io2), 1,000 MB/s throughput
# - Snapshots: Incremental backups to S3
# - Encryption: At-rest encryption (KMS)
# - Latency: ~1ms (network overhead)
# - Cost: $0.08-$0.125 per GB/month (gp3)

# Instance Store - Physically attached (ephemeral)
# - Ephemeral: Data lost on stop/termination (only survives reboot)
# - Performance: Up to 3.3M IOPS, 40 GB/s (i4i.32xlarge)
# - No snapshots: Cannot backup directly
# - Free: Included with instance cost
# - Latency: <100μs (local NVMe SSD)

# When to use EBS:
# - Boot volume (always EBS)
# - Database (need persistence)
# - Stateful apps (data must survive instance failure)

# When to use Instance Store:
# - Temporary data (caches, buffers, scratch space)
# - High IOPS needed (NoSQL replicas, Hadoop, Cassandra)
# - Cost-sensitive (no extra storage charge)
# - Data replicated elsewhere (can rebuild on failure)

# Example: Cassandra cluster
# - 10 nodes, each with 2TB instance store
# - Replication factor = 3 (data on 3 nodes)
# - Node failure: Data rebuilt from replicas
# - Benefits: 3M IOPS, no EBS cost
```

### Instance Types

**Naming**: `c5.2xlarge`
- **Family**: `c` = compute-optimized
- **Generation**: `5` = 5th generation
- **Size**: `2xlarge` = 8 vCPUs, 16GB RAM

#### Families
- **General Purpose** (`t3`, `m5`): Balanced CPU/memory (web servers, dev)
- **Compute Optimized** (`c5`, `c6`): High CPU (batch, ML inference, HPC)
- **Memory Optimized** (`r5`, `x1`): High memory (databases, caching)
- **Storage Optimized** (`i3`, `d2`): High IOPS, throughput (data warehouses, Hadoop)
- **Accelerated Computing** (`p3`, `g4`): GPUs (ML training, video encoding)

#### Burstable Instances (`t2`, `t3`, `t4g`)
- **CPU Credits**: Accumulate credits when idle, spend when busy
- **Baseline**: e.g., `t3.micro` = 10% baseline CPU
- **Burst**: Can burst to 100% using credits
- **Unlimited mode**: Can burst indefinitely (pay overage fee)
- **Use case**: Variable workloads (dev/test, low-traffic sites)

### Pricing Models

#### On-Demand
- **Pay per hour/second** (no commitment)
- **Use case**: Short-term, spiky, unpredictable workloads

#### Reserved Instances (RI)
- **Commitment**: 1 or 3 years
- **Discount**: 40-60% off on-demand
- **Types**:
  - **Standard RI**: Best discount, can't change instance type
  - **Convertible RI**: Can change instance type, less discount
- **Payment**: All upfront, partial upfront, no upfront
- **Use case**: Steady-state workloads (databases, always-on services)

#### Savings Plans
- **Commitment**: $/hour for 1 or 3 years
- **Flexibility**: Can change instance family, size, OS, region
- **Discount**: Similar to RIs (up to 72%)
- **Types**:
  - **Compute Savings Plan**: Most flexible (EC2, Fargate, Lambda)
  - **EC2 Instance Savings Plan**: Less flexible, higher discount

#### Spot Instances
- **Bid on spare capacity**: Up to 90% discount
- **Can be terminated**: AWS reclaims with 2-minute warning
- **Use case**: Fault-tolerant, flexible workloads (batch, big data, CI/CD)
- **Spot Fleet**: Mix of Spot + On-Demand (target capacity)
- **Spot Blocks**: Reserve Spot for 1-6 hours (no interruption)

#### Dedicated Hosts
- **Physical server** dedicated to you
- **Use case**: Compliance, licensing (BYOL - Bring Your Own License)
- **Cost**: Most expensive

#### Dedicated Instances
- **Instances on dedicated hardware** (but hardware not reserved for you)
- **Use case**: Isolation from other customers (compliance)

### Storage

#### EBS (Elastic Block Store)
- **Network-attached** block storage (like hard drive)
- **Persistent**: Data survives instance stop/termination (if configured)
- **Single AZ**: EBS volume tied to one AZ (can snapshot → copy to other AZ)
- **IOPS**: Provisioned or burstable

**Volume Types**:
| Type | IOPS | Throughput | Use Case |
|------|------|------------|----------|
| **gp3** (General Purpose SSD) | 3,000-16,000 | 125-1,000 MB/s | Most workloads (balanced) |
| **gp2** (General Purpose SSD) | 3-16,000 (burstable) | 128-250 MB/s | Legacy, smaller volumes |
| **io2/io1** (Provisioned IOPS SSD) | 64,000+ | 1,000 MB/s | Databases (high IOPS) |
| **st1** (Throughput Optimized HDD) | 500 | 500 MB/s | Big data, data warehouses |
| **sc1** (Cold HDD) | 250 | 250 MB/s | Infrequent access (cheapest) |

**Snapshots**:
- Incremental backups to S3
- Restores create new EBS volume (any AZ)
- Can copy across regions

#### Instance Store
- **Physically attached** storage (ephemeral)
- **High IOPS**: NVMe SSDs (millions of IOPS)
- **Ephemeral**: Data lost on stop/termination
- **Use case**: Temporary data, caching, buffers

#### EFS (Elastic File System)
- **Shared NFS** (multiple instances can mount)
- **Multi-AZ**: Data replicated across AZs
- **Auto-scaling**: Grows/shrinks automatically
- **Use case**: Shared storage, content management, home directories

### Networking

#### Elastic IP (EIP)
- **Static public IP** (doesn't change on stop/start)
- **Free** when attached to running instance
- **Charged** when reserved but not attached
- **Limit**: 5 per region (can request increase)

#### Elastic Network Interface (ENI)
- **Virtual network card** (private IP, public IP, MAC)
- Can **detach and attach** to different instances
- **Use case**: Licensing tied to MAC, failover, multi-NIC

#### Security Groups
- **Virtual firewall** (stateful)
- **Inbound/outbound rules**: Allow traffic (no deny)
- **Default**: All outbound allowed, all inbound denied
- **Best practice**: Least privilege, reference other SGs (not IPs)

#### Placement Groups
- **Cluster**: Instances close together (same AZ, low latency) - HPC
- **Spread**: Instances on different hardware (max 7/AZ) - high availability
- **Partition**: Groups on different racks (big data, Kafka, Cassandra)

### Auto Scaling

#### Auto Scaling Group (ASG)
- **Automatically** add/remove instances based on demand
- **Min/Max/Desired**: Set capacity bounds
- **Scaling policies**:
  - **Target tracking**: Maintain metric (e.g., 50% CPU)
  - **Step scaling**: Add/remove based on CloudWatch alarm thresholds
  - **Scheduled**: Scale at specific times (predictable patterns)
- **Health checks**: EC2 or ELB (replace unhealthy instances)
- **Multi-AZ**: Distribute instances across AZs

#### Launch Templates
- **Blueprint** for instances (AMI, instance type, key pair, SG)
- **Versioned**: Can update and rollback
- Replaces **Launch Configurations** (legacy)

### Monitoring

#### CloudWatch
- **Metrics**: CPU, Network, Disk (5-min default, 1-min detailed)
- **No memory/disk usage by default**: Install CloudWatch agent
- **Logs**: Stream logs to CloudWatch Logs
- **Alarms**: Trigger actions on thresholds

#### Status Checks
- **System status**: AWS infrastructure (reboot to fix)
- **Instance status**: OS/software (stop/start to fix)
- **Auto-recovery**: Automatically recover on failure

### User Data
- **Script** run at **first launch** (bootstrap)
- **Use case**: Install software, configure, fetch secrets
- Runs as **root**

```bash
#!/bin/bash
yum update -y
yum install -y httpd
systemctl start httpd
```

### Metadata
- **Instance metadata**: Info about instance (IP, ID, IAM role)
- **Access**: `http://169.254.169.254/latest/meta-data/`
- **Use case**: Get instance info from within instance

### Limits
- **Instances per region**: 20 vCPUs (on-demand, default) - can increase
- **EBS volumes**: 5,000 per region
- **Snapshots**: 100,000 per region
- **Security groups**: 500 per VPC
- **Rules per SG**: 60 inbound + 60 outbound

### Best Practices
1. **Use latest generation**: Better performance, lower cost
2. **Right-size**: Don't over-provision (use CloudWatch to analyze)
3. **Reserved/Savings Plans**: For steady workloads (40-70% savings)
4. **Spot for fault-tolerant**: Batch, big data (up to 90% savings)
5. **Auto Scaling**: Handle variable load, improve availability
6. **Multi-AZ**: Spread instances across AZs (high availability)
7. **EBS snapshots**: Regular backups
8. **Security groups**: Least privilege, no 0.0.0.0/0 unless necessary
9. **IAM roles**: Use roles instead of access keys
10. **Monitoring**: CloudWatch agent for memory/disk

---

## Fargate

### Overview
- **Serverless compute for containers** (ECS, EKS)
- **No EC2 management**: AWS manages infrastructure (no patching, scaling hosts)
- **Pay per use**: Per vCPU-second and GB-second (no idle cost)
- **Right-sizing**: Specify exact CPU/memory per task (granular pricing)
- **Cold start**: ~30 seconds to launch task (vs EC2 pre-provisioned)

### Fargate Hard Limits & Key Constraints

| Limit | Value | Impact | Solution |
|-------|-------|--------|----------|
| **Task CPU** | 0.25 - 16 vCPU | Cannot exceed 16 vCPU per task | Use multiple tasks or EC2 |
| **Task memory** | 0.5GB - 120GB | Based on CPU (specific combos) | Check supported configs |
| **Ephemeral storage** | 20GB (default), 200GB (max) | Lost when task stops | Use EFS for persistence |
| **ENI per task** | 1 | Each task gets own IP | Plan IP space carefully |
| **Tasks per service** | 1,000 (soft) | Scale limit | Request increase |
| **Container startup** | 10 min timeout | Task fails if > 10 min | Optimize image, split init |
| **EFS throughput** | Depends on EFS class | Slow if many tasks read | Use Provisioned Throughput |
| **Platform version** | Must specify | Old versions deprecated | Use LATEST carefully |

**Cost Comparison Example** (Interview Favorite):
```python
# Scenario: Run 10 containers, each needs 1 vCPU, 2GB RAM, 24/7

# Option 1: Fargate
# - 10 tasks × 1 vCPU × $0.04 × 730 hrs/mo = $292
# - 10 tasks × 2GB × $0.004 × 730 hrs/mo = $58.40
# Total: $350.40/month

# Option 2: EC2 (c5.2xlarge: 8 vCPU, 16GB, $0.34/hr)
# - 2 instances needed (10 tasks / 5 tasks per instance)
# - 2 × $0.34 × 730 = $496.40/month
# - But: Wasted capacity (16 vCPU, using 10)

# Option 3: EC2 with better packing (t3.xlarge: 4 vCPU, 16GB, $0.15/hr)
# - 3 instances (10 tasks / 3-4 per instance)
# - 3 × $0.15 × 730 = $328.50/month
# - Closer to Fargate, but need to manage instances

# Crossover point: ~70% utilization
# - Below 70%: Fargate cheaper (no wasted capacity)
# - Above 70%: EC2 cheaper (economies of scale)

# Fargate Spot: 70% discount
# - 10 tasks × $350.40 × 0.30 = $105/month (cheapest!)
# - Tradeoff: Tasks can be interrupted (2-min warning)
```

### Core Concepts

#### Task Definition
- **Blueprint** for your application (like Dockerfile)
- Specifies:
  - Container image
  - CPU and memory allocation
  - IAM role (task role, execution role)
  - Networking mode (awsvpc only for Fargate)
  - Logging, environment variables

#### Task
- **Running instance** of task definition
- Fargate allocates:
  - **ENI** (Elastic Network Interface) → private IP
  - **vCPU + memory** based on task definition
  - **Ephemeral storage**: 20GB (default, can increase to 200GB)

### CPU and Memory Configurations

**Supported combinations** (CPU | Memory):
- 0.25 vCPU: 0.5GB, 1GB, 2GB
- 0.5 vCPU: 1GB - 4GB (1GB increments)
- 1 vCPU: 2GB - 8GB (1GB increments)
- 2 vCPU: 4GB - 16GB (1GB increments)
- 4 vCPU: 8GB - 30GB (1GB increments)
- 8 vCPU: 16GB - 60GB (4GB increments)
- 16 vCPU: 32GB - 120GB (8GB increments)

**Pricing** (us-east-1 example):
- vCPU: ~$0.04 per vCPU-hour
- Memory: ~$0.004 per GB-hour

### Fargate vs EC2 Launch Type

| Feature | Fargate | EC2 Launch Type |
|---------|---------|-----------------|
| **Management** | Serverless (AWS manages) | You manage EC2 instances |
| **Scaling** | Auto-scales tasks | Need ASG for instances |
| **Cost** | Pay per task (higher per-resource) | Pay per instance (cheaper at scale) |
| **Overhead** | Zero | Patch, monitor, scale instances |
| **Use case** | Small-medium scale, simplicity | Large scale, cost optimization |
| **Cold start** | ~30 seconds (task launch) | Instances pre-provisioned |

### Networking

#### awsvpc Mode (Required)
- Each task gets **own ENI** (Elastic Network Interface)
- **Own private IP** (and optional public IP)
- **Own security group** (task-level isolation)
- **No port conflicts**: Each task has full port range

#### Load Balancer Integration
- **ALB** (Application Load Balancer): HTTP/HTTPS (most common)
- **NLB** (Network Load Balancer): TCP/UDP, low latency
- **Target type**: `ip` (since tasks have ENIs)
- **Dynamic port mapping**: Not needed (each task has full port range)

### Storage

#### Ephemeral Storage
- **Default**: 20GB
- **Max**: 200GB (configurable)
- **Lifecycle**: Lost when task stops
- **Use case**: Temporary files, caching

#### EFS Integration
- Mount **EFS volumes** for persistent storage
- **Shared storage**: Multiple tasks can mount same EFS
- **Use case**: Shared data, stateful apps

### IAM Roles

#### Task Execution Role
- **Used by**: Fargate agent (pulls image, writes logs)
- **Permissions**: ECR (pull images), CloudWatch Logs (write logs), Secrets Manager

#### Task Role
- **Used by**: Your application code
- **Permissions**: S3, DynamoDB, etc. (whatever app needs)

### Auto Scaling

#### Service Auto Scaling
- **Target tracking**: Maintain metric (e.g., 70% CPU, 1000 ALB requests/target)
- **Step scaling**: Add/remove tasks based on CloudWatch alarms
- **Scheduled**: Scale at specific times

**Metrics**:
- `ECSServiceAverageCPUUtilization`
- `ECSServiceAverageMemoryUtilization`
- ALB metrics (`RequestCountPerTarget`)

#### Capacity Providers
- **Fargate**: Serverless, auto-scales
- **Fargate Spot**: Up to 70% discount (can be interrupted)
- **Mix**: Combine Fargate + Fargate Spot (cost optimization)

### Fargate Spot
- **Spot pricing** for Fargate (up to 70% off)
- **Interruption**: AWS can reclaim with 2-minute warning
- **Use case**: Fault-tolerant, batch, dev/test
- **ECS behavior**: Tries to drain gracefully (respects `stopTimeout`)

### Monitoring

#### CloudWatch Container Insights
- **Automatic metrics**: CPU, memory, network, storage (task + service level)
- **Enable**: `--enable-container-insights` on cluster
- **Logs**: Automatic log aggregation (stdout/stderr → CloudWatch Logs)

#### Metrics
- `CPUUtilization`, `MemoryUtilization`: Per task/service
- `RunningTasksCount`, `PendingTasksCount`
- `TargetResponseTime` (ALB), `RequestCount`

### Limits
- **Tasks per service**: 1,000 (soft limit, can increase)
- **Ephemeral storage**: Max 200GB
- **Container startup timeout**: 10 minutes (task fails if not healthy)
- **ENI per task**: 1 (awsvpc mode)

### Best Practices
1. **Right-size**: Match CPU/memory to actual usage (CloudWatch insights)
2. **Fargate Spot**: For fault-tolerant workloads (70% savings)
3. **Health checks**: Configure ALB/NLB health checks (automatic replacement)
4. **Logging**: Use CloudWatch Logs (structured JSON for parsing)
5. **Auto Scaling**: Target tracking on CPU/memory or ALB metrics
6. **EFS for state**: Mount EFS for persistent data
7. **Task role**: Grant least-privilege IAM permissions
8. **Secrets**: Use Secrets Manager/Parameter Store (not env vars)
9. **Multi-AZ**: Deploy tasks across multiple subnets (AZs)
10. **CI/CD**: Blue/green deployments (ECS supports CodeDeploy)

### Common Patterns

#### API Service
- ALB → Fargate tasks (ECS service)
- Auto-scale on CPU/ALB requests
- Multiple AZs for HA

#### Batch Processing
- SQS → Fargate tasks (scale based on queue depth)
- Or EventBridge → ECS RunTask (scheduled jobs)

#### Microservices
- Service mesh (App Mesh) for inter-service communication
- Each microservice = separate ECS service

---

## Redshift

### Overview
- **Data warehouse**: OLAP (analytics), NOT OLTP (transactions)
- **Columnar storage**: Optimized for aggregations, scans (not row-by-row updates)
- **MPP** (Massively Parallel Processing): Distributes queries across nodes
- **Petabyte-scale**: Up to 16 PB per cluster (8 PB per node × 2 nodes with RA3)
- **Cost**: 10× cheaper than running equivalent analytics on RDS/Aurora

### Redshift Hard Limits & Key Constraints

| Limit | Value | Impact | Solution |
|-------|-------|--------|----------|
| **Cluster size** | 1-128 compute nodes | Scale limit | Use multiple clusters or Spectrum |
| **Column count** | 1,600 per table | Wide tables fail | Normalize or use SUPER type |
| **Row size** | 64KB (without BLOB), 4MB (with BLOB) | Large rows fail | Split into multiple tables |
| **COPY file size** | No limit, but 1-125MB per file optimal | Too small/large = slow | Split files to optimal size |
| **Query result** | 1MB per row (JDBC/ODBC) | Large result sets fail | Paginate or use UNLOAD to S3 |
| **Concurrent queries** | 50 (default WLM) | Queuing if exceeded | Tune WLM, use concurrency scaling |
| **Concurrent connections** | 500 (soft), 2,000 (hard) | Connection refused | Use connection pooling |
| **Spectrum partitions** | 20,000 per table | Many partitions slow | Coarser partitions (monthly vs daily) |
| **Databases** | 60 per cluster | Logical separation limit | Use schemas within databases |
| **Snapshot size** | Cluster size (compressed) | Restore takes time | Incremental snapshots faster |
| **VACUUM time** | Hours for large tables | Blocks concurrent writes | Use automatic vacuum, schedule off-peak |

**Critical Interview Topic: Distribution Keys**
```sql
-- Problem: Join two large tables (100M+ rows each)
-- Without proper distribution → massive data shuffle (hours)

-- ❌ Bad: EVEN distribution on both
CREATE TABLE orders (
    order_id BIGINT,
    user_id BIGINT,
    amount DECIMAL
) DISTSTYLE EVEN;  -- Round-robin distribution

CREATE TABLE users (
    user_id BIGINT,
    name VARCHAR(100)
) DISTSTYLE EVEN;

-- Query: Join orders and users
SELECT u.name, SUM(o.amount)
FROM orders o
JOIN users u ON o.user_id = u.user_id
GROUP BY u.name;

-- What happens:
-- 1. orders and users distributed randomly across nodes
-- 2. For join, must shuffle orders data to co-locate with users
-- 3. 100M rows × 64KB = 6.4TB network transfer
-- 4. Query takes 2+ hours (I/O bound)

-- ✅ Good: Match distribution keys on join column
CREATE TABLE orders (
    order_id BIGINT,
    user_id BIGINT,
    amount DECIMAL
) DISTSTYLE KEY DISTKEY (user_id);  -- Hash on user_id

CREATE TABLE users (
    user_id BIGINT,
    name VARCHAR(100)
) DISTSTYLE KEY DISTKEY (user_id);  -- Same key!

-- Now:
-- 1. All orders for user_123 on same node as user_123 record
-- 2. Join is local (no network shuffle)
-- 3. Query takes 30 seconds (100× faster)

-- Golden rule: DISTKEY = frequent join column with high cardinality
```

**Sort Keys and Query Performance**:
```sql
-- Scenario: Queries filter by date range

-- ❌ Without sort key:
CREATE TABLE events (
    event_id BIGINT,
    user_id BIGINT,
    event_date DATE,
    data JSON
);

SELECT * FROM events
WHERE event_date BETWEEN '2024-01-01' AND '2024-01-31';
-- Scans entire table (1TB) → 5 minutes

-- ✅ With sort key on date:
CREATE TABLE events (
    event_id BIGINT,
    user_id BIGINT,
    event_date DATE,
    data JSON
) SORTKEY (event_date);

SELECT * FROM events
WHERE event_date BETWEEN '2024-01-01' AND '2024-01-31';
-- Zone maps skip blocks outside date range
-- Scans only 100GB → 30 seconds (10× faster)

-- Sort key = like an index, but for columnar storage
-- Data physically sorted on disk (not a separate structure)
```

### Architecture

```
Leader Node
├── Query planning, coordination
├── Client connections (JDBC/ODBC)
└── Distributes work to compute nodes

Compute Nodes (1-128)
├── Execute queries in parallel
├── Store data in slices
└── Local storage (SSD or HDD)
```

#### Node Types

| Type | Use Case | Storage | vCPU | Memory |
|------|----------|---------|------|--------|
| **RA3** (Recommended) | Most workloads | Managed storage (S3), scalable | 4-96 | 32-768 GB |
| **DC2** | Compute-intensive, < 10TB | Local SSD | 2-32 | 15-244 GB |
| **DS2** (Legacy) | Large datasets | Local HDD | 4-36 | 30-244 GB |

**RA3 advantages**:
- **Decouple compute and storage**: Scale independently
- **Automatic tiering**: Hot data on local cache, cold on S3
- **Concurrency Scaling**: Auto-add clusters for bursts

### Distribution Styles

**Determines how rows are distributed across nodes** (critical for performance)

#### EVEN (Default)
- **Round-robin** distribution (equal rows per node)
- **Use case**: Tables not joined, small tables
- **Pros**: Simple, balanced
- **Cons**: Joins require data shuffle (slow)

#### KEY
- **Hash** on specified column (same key → same node)
- **Use case**: Large fact tables joined on specific key
- **Pros**: Co-locates join data (faster joins)
- **Cons**: Skew if key poorly distributed

```sql
CREATE TABLE orders (
  order_id INT,
  user_id INT,
  amount DECIMAL
) DISTSTYLE KEY DISTKEY (user_id);
```

#### ALL
- **Full copy** of table on every node
- **Use case**: Small dimension tables (< 3M rows)
- **Pros**: No shuffle on joins
- **Cons**: Wastes space, slow writes

#### AUTO
- Redshift chooses based on table size
- Small → ALL, Large → EVEN or KEY

### Sort Keys

**Determines physical order of data on disk** (like index)

#### Compound Sort Key (Default)
- **Multiple columns** in order of priority
- **Prefix matching**: Query must use first column(s) for benefit
- **Use case**: Queries filter on specific columns in order

```sql
CREATE TABLE events (
  user_id INT,
  event_time TIMESTAMP,
  event_type VARCHAR(50)
) SORTKEY (user_id, event_time);

-- Fast: WHERE user_id = 123 AND event_time > '2024-01-01'
-- Slow: WHERE event_time > '2024-01-01' (doesn't use sort key prefix)
```

#### Interleaved Sort Key
- **Equal weight** to all columns
- **Any combination** of columns benefits
- **Use case**: Multiple query patterns
- **Cons**: Slower VACUUM, less efficient inserts

#### AUTO
- Redshift manages sort key based on query patterns

### Compression (Encoding)

- **Automatic**: Redshift chooses encoding based on data
- **Reduces storage**: 3-10× compression typical
- **Improves I/O**: Less data to scan
- **Types**: LZO, ZSTD, Byte Dictionary, Run-length, Delta, etc.

```sql
-- Let Redshift choose
CREATE TABLE mytable (col1 INT, col2 VARCHAR(100));

-- Manual encoding
CREATE TABLE mytable (
  col1 INT ENCODE LZO,
  col2 VARCHAR(100) ENCODE ZSTD
);
```

### Concurrency Scaling

- **Auto-add clusters** during high concurrency
- **Transparent**: Queries routed automatically
- **Pricing**: Free credits (1 hour/day), then hourly
- **Use case**: Handle burst read queries (BI dashboards)

### Workload Management (WLM)

- **Query queues**: Separate pools for different workloads
- **Auto WLM** (Recommended): Redshift manages queues
- **Manual WLM**: Define queues, memory %, concurrency
- **Short Query Acceleration (SQA)**: Fast-track short queries

**Example**: Separate queues for ETL (high memory, low concurrency) and BI (low memory, high concurrency)

### Redshift Spectrum

- **Query S3 data** directly (without loading)
- **External tables**: Define schema, point to S3
- **Use case**: Infrequent data, extend warehouse to data lake
- **Format**: Parquet, ORC, JSON, CSV, Avro
- **Partitioning**: Partition S3 data by date, region, etc. (faster queries)

```sql
CREATE EXTERNAL TABLE spectrum.sales (
  sale_id INT,
  amount DECIMAL,
  sale_date DATE
)
STORED AS PARQUET
LOCATION 's3://my-bucket/sales/'
PARTITION BY (year INT, month INT);
```

### Data Loading

#### COPY Command (Recommended)
- **Fastest**: Parallel load from S3, DynamoDB, EMR
- **Compression**: Auto-detects GZIP, BZIP2, etc.
- **Encryption**: Supports SSE-S3, SSE-KMS
- **Manifest**: List files to load (ensures exactly-once)

```sql
COPY sales
FROM 's3://my-bucket/sales/'
IAM_ROLE 'arn:aws:iam::123456789012:role/RedshiftRole'
FORMAT AS PARQUET;
```

#### INSERT
- Slower than COPY (use for small batches)

#### AWS Data Pipeline, Glue
- Orchestrate ETL jobs → COPY to Redshift

### Backup and Snapshots

#### Automated Snapshots
- **Daily backups** of cluster (incremental)
- **Retention**: 1-35 days (default 1)
- **Free**: Stored in S3 (up to cluster size)

#### Manual Snapshots
- **On-demand** backups (persist until deleted)
- **Cross-region copy**: For DR

#### Restore
- Creates **new cluster** from snapshot
- Cannot restore in-place

### Monitoring

#### Query Performance
- **Console**: Query monitoring, execution details
- **System Tables**: `STL_QUERY`, `SVL_QUERY_SUMMARY`, `STL_ALERT_EVENT_LOG`
- **CloudWatch**: CPU, disk I/O, network, query duration

#### Common Issues
- **Disk skew**: Uneven data distribution (check `SVV_TABLE_INFO`)
- **Long-running queries**: Check `STL_QUERY`, look for sorts, scans
- **VACUUM/ANALYZE**: Reclaim space, update statistics (auto runs by default)

### Limits
- **Nodes**: 1-128 compute nodes per cluster
- **Databases**: 60 per cluster
- **Tables**: 9,900 per database
- **Columns**: 1,600 per table
- **Concurrent connections**: 500 (can increase to 2,000)
- **Query result size**: 1MB per row (JDBC/ODBC)

### Security
- **Encryption at rest**: KMS or HSM
- **Encryption in transit**: SSL/TLS (JDBC/ODBC)
- **VPC**: Deploy in VPC (private subnets)
- **IAM**: Role-based access for COPY, Spectrum
- **Database users**: CREATE USER, GRANT permissions

### Best Practices
1. **Distribution key**: Choose key with high cardinality, even distribution
2. **Sort key**: Match frequent query filters (date ranges, IDs)
3. **Use COPY**: 10-100× faster than INSERT
4. **Compression**: Let Redshift auto-encode (or run ANALYZE COMPRESSION)
5. **VACUUM**: Auto-runs, reclaims space from deletes
6. **ANALYZE**: Update statistics after bulk load (auto-runs)
7. **RA3 nodes**: For flexibility (decouple compute/storage)
8. **Concurrency Scaling**: Enable for burst queries
9. **Spectrum**: For cold/infrequent data (keep hot data in Redshift)
10. **Workload isolation**: Use WLM queues to separate ETL and BI

### Common Patterns

#### Data Warehouse
- S3 (data lake) → Glue ETL → Redshift (warehouse) → BI tools (QuickSight, Tableau)

#### Federated Query
- Redshift → RDS/Aurora (query operational DBs directly, no ETL)

#### Materialized Views
- Pre-compute aggregations (auto-refresh on data change)

---

## Glue

### Overview
- **Fully managed ETL** (Extract, Transform, Load)
- **Serverless**: No infrastructure to manage (Apache Spark under hood)
- **Data Catalog**: Centralized metadata repository (shared across Athena, Redshift Spectrum, EMR)
- **Crawlers**: Auto-discover schemas from S3, RDS, Redshift, DynamoDB
- **Language**: PySpark (Python) or Scala

### Glue Hard Limits & Key Constraints

| Limit | Value | Impact | Solution |
|-------|-------|--------|----------|
| **Job timeout** | 48 hours max | Long jobs fail | Split into smaller jobs |
| **DPU per job** | 2-100 (Standard), up to 1,000 | Scale limit for large ETL | Request increase or split job |
| **Concurrent jobs** | 25 (can increase to 100+) | Job queuing | Request increase or stagger |
| **Crawler runtime** | No hard limit, but costs accrue | Expensive for large datasets | Partition data, incremental crawl |
| **Data Catalog objects** | 1M free, then $1 per 100K/mo | Cost for large catalogs | Archive unused tables |
| **Partitions per table** | 20M | Extremely partitioned data | Coarser partitions |
| **Glue connection** | 1,000 per account | Cannot create more | Clean up unused |
| **Job bookmark** | Per transform | Only tracks processed files | Custom checkpoint for complex logic |
| **DynamicFrame** | In-memory limit (based on DPU) | OOM for huge datasets | Repartition or use streaming |

**Critical Concepts: DPU (Data Processing Unit)**
```python
# 1 DPU = 4 vCPU + 16 GB memory + 64 GB disk (SSD)

# Job types:
# - Standard (Spark): Min 2 DPU, max 100
# - Python Shell: 0.0625 or 1 DPU (lightweight, no Spark)
# - Streaming: Auto-scales (1-100 DPU)

# Cost: $0.44 per DPU-hour

# Example: Process 100GB CSV → Parquet
# Estimated: 10 DPU × 1 hour = $4.40
# Savings: 10× compression (100GB → 10GB)
# Future queries (Athena): 10× faster, 90% cheaper

# Choosing DPU count:
# - Small jobs (< 1GB): 2-5 DPU
# - Medium (1-100GB): 10-20 DPU
# - Large (100GB-1TB): 50-100 DPU
# - Very large (> 1TB): 100+ DPU or split job

# Auto-scaling (Glue 3.0+):
glue.create_job(
    Name='my-etl',
    Role='GlueRole',
    Command={'Name': 'glueetl', 'ScriptLocation': 's3://...'},
    GlueVersion='3.0',
    WorkerType='G.1X',  # 1 DPU
    NumberOfWorkers=10,  # Initial workers
    ExecutionProperty={
        'MaxConcurrentRuns': 3
    },
    # Auto-scaling
    MaxCapacity=50  # Can scale up to 50 DPU
)
```

**Job Bookmarks (Incremental ETL)**:
```python
# Problem: Daily ETL job, don't want to reprocess old data

# ❌ Without bookmarks: Process all data every time
# Day 1: Process 1GB
# Day 2: Process 2GB (1GB old + 1GB new) → waste
# Day 30: Process 30GB (29GB already processed)

# ✅ With bookmarks: Track what's been processed
glue_job = glue.create_job(
    Name='incremental-etl',
    Command={'Name': 'glueetl', 'ScriptLocation': 's3://script.py'},
    DefaultArguments={
        '--job-bookmark-option': 'job-bookmark-enable'
    }
)

# In script:
from awsglue.context import GlueContext
from awsglue.job import Job

glueContext = GlueContext(SparkContext.getOrCreate())
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# Read with bookmark (automatically filters to new data only)
datasource = glueContext.create_dynamic_frame.from_catalog(
    database="my_db",
    table_name="my_table",
    transformation_ctx="datasource"  # Bookmark tracked by this ctx
)

# Transform...
output = datasource.apply_mapping([...])

# Write
glueContext.write_dynamic_frame.from_catalog(
    frame=output,
    database="output_db",
    table_name="output_table",
    transformation_ctx="output"  # Bookmark updated on success
)

job.commit()  # Mark bookmark as successful

# How it works:
# - Tracks: S3 files processed, DynamoDB checkpoint, JDBC high watermark
# - Next run: Only processes NEW files since last bookmark
# - Result: 1GB/day instead of cumulative
```

**Partitioning Best Practice** (Interview Favorite):
```python
# Problem: 1 year of data, queries filter by date

# ❌ Bad: No partitions
# s3://data/events.parquet (365GB in one file)
# Query for Jan 1: Scans entire 365GB file → $18.25 (Athena cost)

# ❌ Bad: Too many partitions (hourly)
# s3://data/year=2024/month=01/day=01/hour=00/events.parquet
# s3://data/year=2024/month=01/day=01/hour=01/events.parquet
# ...
# s3://data/year=2024/month=12/day=31/hour=23/events.parquet
# Total: 365 days × 24 hours = 8,760 partitions
# Glue crawler: 30 min to scan, 8,760 API calls
# Athena query: Lists all partitions → slow metadata query

# ✅ Good: Daily partitions
# s3://data/year=2024/month=01/day=01/events.parquet (1GB)
# s3://data/year=2024/month=01/day=02/events.parquet (1GB)
# Total: 365 partitions
# Query for Jan 1: Scans only 1GB → $0.05 (365× cheaper)
# Crawler: 2 min to scan
# Athena: Fast metadata lookup

# Rule of thumb:
# - Each partition: 128MB - 1GB (optimal file size)
# - Total partitions: < 10,000 (for good performance)
# - Partition by most common filter (date, region, etc.)
```

### Components

#### Glue Data Catalog
- **Metadata repository**: Central location for table definitions
- **Databases**: Logical grouping of tables
- **Tables**: Schema (columns, types, partitions, location)
- **Partitions**: Logical divisions (e.g., by date)
- **Integrations**: Athena, Redshift Spectrum, EMR, Glue ETL

**Why it matters**: Single source of truth for schema (instead of managing in each service)

#### Crawlers
- **Auto-discover schemas** from data sources
- **Supports**: S3, RDS, DynamoDB, JDBC databases
- **Creates**: Tables in Data Catalog
- **Partitions**: Auto-detects partition structure (e.g., `s3://bucket/year=2024/month=01/`)
- **Scheduling**: Run on-demand or schedule (cron)

**Example**: Crawler scans `s3://logs/year=2024/month=01/` → creates table with `year`, `month` partitions

#### Glue ETL Jobs
- **Transform data**: Python or Scala (Apache Spark)
- **Serverless**: Auto-scales workers (DPUs - Data Processing Units)
- **Sources**: S3, JDBC, Kafka, Kinesis, Data Catalog
- **Targets**: S3, JDBC, Data Catalog
- **Job types**:
  - **Spark**: Batch ETL (Python/Scala)
  - **Streaming**: Real-time (Kinesis, Kafka)
  - **Python Shell**: Lightweight jobs (no Spark)

**DPU (Data Processing Unit)**:
- 1 DPU = 4 vCPU + 16 GB memory
- Min 2 DPUs (Standard), 0.25 DPU (Python Shell)
- Auto-scaling: Can scale workers dynamically

#### Glue Studio
- **Visual ETL**: Drag-and-drop interface (no code)
- **Generates code**: Creates PySpark scripts
- **Use case**: Simplify ETL for non-developers

#### Glue DataBrew
- **Visual data preparation**: Clean, normalize data (no code)
- **250+ transformations**: Remove duplicates, fill nulls, format dates
- **Use case**: Data analysts, data cleaning

### Glue ETL Job Workflow

```
Source (S3, JDBC)
  → DynamicFrame (Glue abstraction over Spark DataFrame)
  → Transformations (filter, join, map, aggregate)
  → Write to Target (S3, Redshift, JDBC)
```

**DynamicFrame**: Like Spark DataFrame but handles schema mismatches, nested data

### Common Transformations
- **ApplyMapping**: Rename, cast columns
- **Filter**: Remove rows based on condition
- **Join**: Combine two DynamicFrames
- **DropFields**: Remove columns
- **Relationalize**: Flatten nested JSON
- **ResolveChoice**: Handle schema conflicts (cast, project)

### Glue Triggers
- **Orchestrate jobs**: Schedule or event-based
- **Types**:
  - **Scheduled**: Cron expression
  - **On-demand**: Manual trigger
  - **Conditional**: After other jobs finish (DAG workflow)
- **Use case**: Multi-step ETL pipelines

### Glue Workflows
- **DAG of jobs**: Define dependencies (job A → job B → job C)
- **Triggers + Crawlers + Jobs**: Combine into workflow
- **Visualization**: See pipeline in Glue console

### Development Endpoints
- **Interactive development**: Jupyter notebook or Zeppelin
- **Test ETL code**: Before running full job
- **Cost**: Pay per hour endpoint is active

### Glue Schema Registry
- **Schema versioning**: Store Avro, JSON, Protobuf schemas
- **Compatibility checks**: Ensure schema evolution doesn't break
- **Integrations**: MSK (Kafka), Kinesis Data Streams
- **Use case**: Ensure producer/consumer schema compatibility

### Monitoring

#### CloudWatch Metrics
- `glue.driver.jvm.heap.usage`: Driver memory
- `glue.executors.aggregate.numRunningTasks`: Active tasks
- Job duration, DPU hours

#### Job Bookmarks
- **Track processed data**: Avoid reprocessing same data
- **Incremental ETL**: Only process new/changed data
- **State**: Stored in Glue (remembers last processed position)

#### Logs
- **CloudWatch Logs**: Driver, executor, progress logs
- **Continuous logging**: Stream logs in real-time (enable in job)

### Pricing
- **Crawlers**: $0.44 per DPU-hour (min 10 min)
- **ETL jobs**: $0.44 per DPU-hour (min 1 min, billed per second)
- **Data Catalog**: Free up to 1M objects, then $1 per 100K/month
- **Development endpoints**: $0.44 per DPU-hour

### Limits
- **Job timeout**: Max 48 hours (can set lower)
- **Concurrent jobs**: 25 (can increase to 100+)
- **DPUs per job**: 2-100 (standard), up to 1,000 (request increase)
- **Crawler concurrent runs**: 10

### Best Practices
1. **Job bookmarks**: Enable for incremental processing (avoid reprocessing)
2. **Partitions**: Partition S3 data by date, region (faster queries, crawlers)
3. **Parquet/ORC**: Use columnar formats (compression, performance)
4. **Pushdown predicates**: Filter early (reduce data shuffling)
5. **Worker type**: Choose based on workload (G.1X, G.2X, G.4X, G.8X)
6. **Auto-scaling**: Enable for variable workloads (cost optimization)
7. **Data Catalog**: Centralize schema (use with Athena, Redshift Spectrum)
8. **Error handling**: Use try-catch, DLQ for failed records
9. **Monitoring**: Enable continuous logging, CloudWatch alarms
10. **Testing**: Use dev endpoints or small job runs to test

### Common Patterns

#### S3 Data Lake ETL
1. **Crawler**: Scan S3 → create tables in Data Catalog
2. **Glue Job**: Transform (deduplicate, clean, join) → write Parquet to S3
3. **Athena**: Query processed data using Data Catalog

#### Database Migration
- RDS/on-prem DB → Glue ETL → S3 (data lake)
- Or → Redshift (data warehouse)

#### Streaming ETL
- Kinesis Data Streams → Glue Streaming Job → S3/Redshift
- Real-time transformations (filter, aggregate)

#### Change Data Capture (CDC)
- DynamoDB Streams → Glue → S3 (archive changes)
- Or RDS (binlog) → Glue → data lake

---

## Summary Table: When to Use Each Service

| Service | Use Case | Key Feature |
|---------|----------|-------------|
| **DynamoDB** | Low-latency NoSQL, session store, user profiles | Single-digit ms, infinite scale |
| **S3** | Object storage, data lake, backups, static hosting | 11 nines durability, unlimited storage |
| **Kinesis** | Real-time streaming, log/event ingestion | Real-time, ordered, replay |
| **SQS** | Decouple services, async processing, buffering | Fully managed queue, unlimited throughput |
| **SNS** | Pub/sub, fan-out notifications, alerts | Push-based, many subscribers |
| **EC2** | Full control, custom OS/software, legacy apps | Flexible, wide instance types |
| **Fargate** | Containerized apps, microservices (no server mgmt) | Serverless containers, pay-per-use |
| **Redshift** | Data warehouse, OLAP analytics, BI queries | Columnar, petabyte-scale, MPP |
| **Glue** | ETL, data catalog, schema discovery | Serverless ETL, auto-discover schema |

---

## Integration Patterns

### Pattern 1: Real-Time Analytics Pipeline
```
Kinesis Data Streams → Lambda → DynamoDB (aggregates)
                    → Kinesis Firehose → S3 → Athena (ad-hoc queries)
                                        → Redshift (BI dashboards)
```

### Pattern 2: Event-Driven Architecture
```
S3 (upload) → SNS Topic → SQS Queue 1 (thumbnail service on Fargate)
                       → SQS Queue 2 (metadata extraction on Lambda)
                       → SQS Queue 3 (virus scan on EC2)
```

### Pattern 3: Data Lake
```
Sources (logs, DBs, apps) → Kinesis/Glue → S3 (Raw)
                                         → Glue ETL → S3 (Processed/Parquet)
                                                   → Athena (queries)
                                                   → Redshift Spectrum (joins with warehouse)
```

### Pattern 4: Microservices
```
ALB → Fargate (Service A) → DynamoDB
                          → SQS → Fargate (Service B)
                                        → SNS → Lambda (notifications)
```

### Pattern 5: Batch Processing
```
S3 (input data) → EventBridge (scheduled) → Fargate (batch job)
                                          → Result → S3 → SNS (alert)
```

---

## Interview Cheat Sheet

### DynamoDB
- **Indexes**: GSI (different keys, eventual consistent, separate RCU/WCU), LSI (same PK, created at table creation)
- **Hot partition**: Uneven key distribution → throttling (solution: add randomness to PK)
- **Query vs Scan**: Query needs PK (efficient), Scan reads all (expensive)

### S3
- **Consistency**: Strong read-after-write (since Dec 2020)
- **Performance**: 5,500 GET/s per prefix (use many prefixes)
- **Multipart**: Required for >5GB, recommended for >100MB

### Kinesis
- **Shard**: 1MB/s write, 2MB/s read (shared) or 2MB/s per consumer (EFO)
- **Ordering**: Within shard (partition key determines shard)
- **Firehose**: Near real-time (60s buffer), no storage, auto-scales

### SQS
- **Standard**: At-least-once, no order
- **FIFO**: Exactly-once, strict order (300 msg/s, 3,000 with batching)
- **Visibility timeout**: Hides message during processing (default 30s)

### Redshift
- **Distribution**: KEY (co-locate joins), ALL (small tables), EVEN (default)
- **Sort key**: Physical order (like index)
- **COPY**: Fastest load (parallel from S3)

### Glue
- **Crawler**: Auto-discover schemas → Data Catalog
- **ETL Jobs**: Serverless Spark (PySpark/Scala)
- **Bookmarks**: Track processed data (incremental ETL)

### Fargate
- **Serverless containers**: No EC2 management
- **Task**: Gets own ENI, security group
- **Spot**: 70% discount (can be interrupted)

### EC2
- **Instance types**: General (t3,m5), Compute (c5), Memory (r5), Storage (i3)
- **Spot**: 90% discount (interruptible)
- **Auto Scaling**: Add/remove instances based on demand
