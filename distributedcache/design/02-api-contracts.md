# Redis API & Command Reference

> Complete reference for Redis's 400+ commands, organized by data type and function.
> Commands marked with ⭐ are covered in the [interview simulation](01-interview-simulation.md).

---

## Table of Contents

1. [String Commands](#1-string-commands)
2. [List Commands](#2-list-commands)
3. [Set Commands](#3-set-commands)
4. [Sorted Set Commands](#4-sorted-set-commands)
5. [Hash Commands](#5-hash-commands)
6. [Key Commands](#6-key-commands)
7. [Expiry Commands](#7-expiry-commands)
8. [HyperLogLog Commands](#8-hyperloglog-commands)
9. [Bitmap Commands](#9-bitmap-commands)
10. [Geospatial Commands](#10-geospatial-commands)
11. [Stream Commands](#11-stream-commands)
12. [Pub/Sub Commands](#12-pubsub-commands)
13. [Transaction Commands](#13-transaction-commands)
14. [Scripting & Functions](#14-scripting--functions)
15. [Connection Commands](#15-connection-commands)
16. [Server / Admin Commands](#16-server--admin-commands)
17. [ACL Commands](#17-acl-commands-60)
18. [Cluster Commands](#18-cluster-commands)
19. [Replication Commands](#19-replication-commands)
20. [Module Commands](#20-module-commands-40)
21. [Memcached API Comparison](#memcached-api-comparison)
22. [RESP Protocol](#resp-protocol)

---

## 1. String Commands

Strings are the most basic Redis value type. A string value can be at most 512 MB.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **GET** ⭐ | `GET key` | O(1) | Get the string value of a key |
| **SET** ⭐ | `SET key value [EX sec] [PX ms] [EXAT ts] [PXAT ts] [NX\|XX] [KEEPTTL] [GET]` | O(1) | Set a string value with optional expiry, conditional flags, and return-old-value |
| **MGET** | `MGET key [key ...]` | O(N) | Get the values of all given keys atomically |
| **MSET** | `MSET key value [key value ...]` | O(N) | Set multiple key-value pairs atomically |
| **MSETNX** | `MSETNX key value [key value ...]` | O(N) | Set multiple keys only if none of them exist (all-or-nothing) |
| **INCR** ⭐ | `INCR key` | O(1) | Atomically increment the integer value of a key by 1 |
| **DECR** | `DECR key` | O(1) | Atomically decrement the integer value of a key by 1 |
| **INCRBY** | `INCRBY key increment` | O(1) | Atomically increment the integer value of a key by a given amount |
| **DECRBY** | `DECRBY key decrement` | O(1) | Atomically decrement the integer value of a key by a given amount |
| **INCRBYFLOAT** | `INCRBYFLOAT key increment` | O(1) | Atomically increment the float value of a key by a given amount |
| **APPEND** | `APPEND key value` | O(1) amortized | Append a value to the end of a string; create key if it does not exist |
| **STRLEN** | `STRLEN key` | O(1) | Get the length of the string value stored at a key |
| **GETRANGE** | `GETRANGE key start end` | O(N) where N = returned length | Get a substring of the string stored at a key |
| **SETRANGE** | `SETRANGE key offset value` | O(1) if no realloc, O(M) otherwise | Overwrite part of a string at the given byte offset |
| **SETNX** | `SETNX key value` | O(1) | **Deprecated** -- use `SET key value NX`. Set key only if it does not exist |
| **SETEX** | `SETEX key seconds value` | O(1) | **Deprecated** -- use `SET key value EX seconds`. Set key with an expiry in seconds |
| **PSETEX** | `PSETEX key milliseconds value` | O(1) | **Deprecated** -- use `SET key value PX ms`. Set key with an expiry in milliseconds |
| **GETDEL** | `GETDEL key` | O(1) | Get the value of a key and delete it atomically |
| **GETEX** | `GETEX key [EX sec] [PX ms] [EXAT ts] [PXAT ts] [PERSIST]` | O(1) | Get the value of a key and optionally set/clear its expiration |
| **LCS** | `LCS key1 key2 [LEN] [IDX] [MINMATCHLEN len] [WITHMATCHLEN]` | O(N*M) | **7.0+**. Longest Common Substring between two string values |

**SET option details:**
- `EX seconds` -- set expiry in seconds
- `PX milliseconds` -- set expiry in milliseconds
- `EXAT unix-time-seconds` -- set expiry as absolute UNIX timestamp (seconds)
- `PXAT unix-time-milliseconds` -- set expiry as absolute UNIX timestamp (milliseconds)
- `NX` -- only set if key does **not** exist (used for distributed locks) ⭐
- `XX` -- only set if key already exists
- `KEEPTTL` -- retain the existing TTL of the key
- `GET` -- return the old value stored at the key (like GETSET)

---

## 2. List Commands

Lists are linked lists of string values. Useful for queues, stacks, and capped collections.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **LPUSH** ⭐ | `LPUSH key element [element ...]` | O(1) per element | Insert one or more elements at the head of a list |
| **RPUSH** ⭐ | `RPUSH key element [element ...]` | O(1) per element | Insert one or more elements at the tail of a list |
| **LPUSHX** | `LPUSHX key element [element ...]` | O(1) per element | Insert elements at the head only if the list already exists |
| **RPUSHX** | `RPUSHX key element [element ...]` | O(1) per element | Insert elements at the tail only if the list already exists |
| **LPOP** ⭐ | `LPOP key [count]` | O(N) where N = count | Remove and return elements from the head of a list |
| **RPOP** ⭐ | `RPOP key [count]` | O(N) where N = count | Remove and return elements from the tail of a list |
| **LLEN** | `LLEN key` | O(1) | Get the length of a list |
| **LRANGE** ⭐ | `LRANGE key start stop` | O(S+N) S=offset, N=range | Get a range of elements from a list |
| **LINDEX** | `LINDEX key index` | O(N) where N = index | Get an element by its index in the list |
| **LSET** | `LSET key index element` | O(N) where N = index | Set the value of an element by its index |
| **LINSERT** | `LINSERT key BEFORE\|AFTER pivot element` | O(N) where N = elements to traverse | Insert an element before or after another element in the list |
| **LREM** | `LREM key count element` | O(N+M) N=length, M=removed | Remove elements matching the given value from a list |
| **LTRIM** | `LTRIM key start stop` | O(N) where N = removed elements | Trim a list to the specified range (capped collections) |
| **BLPOP** | `BLPOP key [key ...] timeout` | O(N) where N = number of keys | Blocking LPOP -- block until an element is available or timeout |
| **BRPOP** | `BRPOP key [key ...] timeout` | O(N) where N = number of keys | Blocking RPOP -- block until an element is available or timeout |
| **LPOS** | `LPOS key element [RANK rank] [COUNT count] [MAXLEN len]` | O(N) | Return the index of matching elements in the list |
| **LMOVE** | `LMOVE source destination LEFT\|RIGHT LEFT\|RIGHT` | O(1) | Atomically pop from one list and push to another (replaces RPOPLPUSH) |
| **BLMOVE** | `BLMOVE source destination LEFT\|RIGHT LEFT\|RIGHT timeout` | O(1) | Blocking version of LMOVE |
| **LMPOP** | `LMPOP numkeys key [key ...] LEFT\|RIGHT [COUNT count]` | O(N+M) | **7.0+**. Pop elements from the first non-empty list among multiple keys |
| **BLMPOP** | `BLMPOP timeout numkeys key [key ...] LEFT\|RIGHT [COUNT count]` | O(N+M) | **7.0+**. Blocking version of LMPOP |

---

## 3. Set Commands

Sets are unordered collections of unique strings. Useful for tags, unique visitors, and set operations.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **SADD** ⭐ | `SADD key member [member ...]` | O(1) per member | Add one or more members to a set |
| **SREM** | `SREM key member [member ...]` | O(N) where N = members to remove | Remove one or more members from a set |
| **SISMEMBER** | `SISMEMBER key member` | O(1) | Test if a member exists in the set |
| **SMISMEMBER** | `SMISMEMBER key member [member ...]` | O(N) where N = members | Test if multiple members exist in the set (batch SISMEMBER) |
| **SMEMBERS** ⭐ | `SMEMBERS key` | O(N) where N = set size | Get all members of a set |
| **SCARD** | `SCARD key` | O(1) | Get the number of members in a set |
| **SPOP** | `SPOP key [count]` | O(N) where N = count | Remove and return one or more random members from a set |
| **SRANDMEMBER** | `SRANDMEMBER key [count]` | O(N) where N = count | Return one or more random members without removing them |
| **SUNION** ⭐ | `SUNION key [key ...]` | O(N) where N = total elements across all sets | Return the union of multiple sets |
| **SINTER** ⭐ | `SINTER key [key ...]` | O(N*M) worst case, N=smallest set, M=number of sets | Return the intersection of multiple sets |
| **SDIFF** | `SDIFF key [key ...]` | O(N) where N = total elements across all sets | Return the difference between the first set and the others |
| **SUNIONSTORE** | `SUNIONSTORE destination key [key ...]` | O(N) | Store the union of multiple sets into a destination set |
| **SINTERSTORE** | `SINTERSTORE destination key [key ...]` | O(N*M) | Store the intersection of multiple sets into a destination set |
| **SDIFFSTORE** | `SDIFFSTORE destination key [key ...]` | O(N) | Store the difference of multiple sets into a destination set |
| **SINTERCARD** | `SINTERCARD numkeys key [key ...] [LIMIT limit]` | O(N*M) worst case | **7.0+**. Return the cardinality of the intersection (count only, no members returned) |
| **SSCAN** | `SSCAN key cursor [MATCH pattern] [COUNT count]` | O(1) per call, O(N) full iteration | Incrementally iterate over the members of a set |

---

## 4. Sorted Set Commands

Sorted sets are sets where each member has an associated score, maintaining order by score. Useful for leaderboards, priority queues, and time-series windows.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **ZADD** ⭐ | `ZADD key [NX\|XX] [GT\|LT] [CH] [INCR] score member [score member ...]` | O(log N) per element | Add one or more members to a sorted set, or update the score if it exists |
| **ZREM** | `ZREM key member [member ...]` | O(M*log N) M=removed | Remove one or more members from a sorted set |
| **ZSCORE** | `ZSCORE key member` | O(1) | Get the score of a member in a sorted set |
| **ZMSCORE** | `ZMSCORE key member [member ...]` | O(N) where N = members | Get scores of multiple members in a sorted set (batch ZSCORE) |
| **ZRANK** ⭐ | `ZRANK key member` | O(log N) | Get the rank (0-based, ascending by score) of a member |
| **ZREVRANK** | `ZREVRANK key member` | O(log N) | Get the rank (0-based, descending by score) of a member |
| **ZRANGE** ⭐ | `ZRANGE key min max [BYSCORE\|BYLEX] [REV] [LIMIT offset count]` | O(log N + M) M=returned | **Unified 6.2+**. Return members in a range by index, score, or lex (replaces ZRANGEBYSCORE, ZRANGEBYLEX, ZREVRANGEBYSCORE, ZREVRANGEBYLEX) |
| **ZRANGESTORE** | `ZRANGESTORE dst src min max [BYSCORE\|BYLEX] [REV] [LIMIT offset count]` | O(log N + M) | Store the result of ZRANGE into a destination sorted set |
| **ZCARD** | `ZCARD key` | O(1) | Get the number of members in a sorted set |
| **ZCOUNT** | `ZCOUNT key min max` | O(log N) | Count members with scores within the given range |
| **ZLEXCOUNT** | `ZLEXCOUNT key min max` | O(log N) | Count members in a sorted set between a lexicographic range (all scores must be equal) |
| **ZINCRBY** | `ZINCRBY key increment member` | O(log N) | Increment the score of a member in a sorted set |
| **ZPOPMIN** | `ZPOPMIN key [count]` | O(log N * M) M=popped | Remove and return members with the lowest scores |
| **ZPOPMAX** | `ZPOPMAX key [count]` | O(log N * M) M=popped | Remove and return members with the highest scores |
| **BZPOPMIN** | `BZPOPMIN key [key ...] timeout` | O(log N) | Blocking version of ZPOPMIN |
| **BZPOPMAX** | `BZPOPMAX key [key ...] timeout` | O(log N) | Blocking version of ZPOPMAX |
| **ZRANDMEMBER** | `ZRANDMEMBER key [count [WITHSCORES]]` | O(N) where N = count | Return one or more random members from a sorted set |
| **ZUNIONSTORE** | `ZUNIONSTORE destination numkeys key [key ...] [WEIGHTS w ...] [AGGREGATE SUM\|MIN\|MAX]` | O(N) + O(M*log M) | Store the union of multiple sorted sets into a destination key |
| **ZINTERSTORE** | `ZINTERSTORE destination numkeys key [key ...] [WEIGHTS w ...] [AGGREGATE SUM\|MIN\|MAX]` | O(N*K) + O(M*log M) | Store the intersection of multiple sorted sets into a destination key |
| **ZDIFFSTORE** | `ZDIFFSTORE destination numkeys key [key ...]` | O(L + (N-K)*log N) | Store the difference of sorted sets into a destination key |
| **ZUNION** | `ZUNION numkeys key [key ...] [WEIGHTS w ...] [AGGREGATE SUM\|MIN\|MAX] [WITHSCORES]` | O(N) + O(M*log M) | Return the union of multiple sorted sets (no store) |
| **ZINTER** | `ZINTER numkeys key [key ...] [WEIGHTS w ...] [AGGREGATE SUM\|MIN\|MAX] [WITHSCORES]` | O(N*K) + O(M*log M) | Return the intersection of multiple sorted sets (no store) |
| **ZDIFF** | `ZDIFF numkeys key [key ...] [WITHSCORES]` | O(L + (N-K)*log N) | Return the difference of multiple sorted sets (no store) |
| **ZMPOP** | `ZMPOP numkeys key [key ...] MIN\|MAX [COUNT count]` | O(K) + O(M*log N) | **7.0+**. Pop members with min/max scores from the first non-empty sorted set |
| **BZMPOP** | `BZMPOP timeout numkeys key [key ...] MIN\|MAX [COUNT count]` | O(K) + O(M*log N) | **7.0+**. Blocking version of ZMPOP |
| **ZSCAN** | `ZSCAN key cursor [MATCH pattern] [COUNT count]` | O(1) per call, O(N) full iteration | Incrementally iterate over members and scores of a sorted set |

**ZADD option details:**
- `NX` -- only add new elements; do not update existing elements
- `XX` -- only update existing elements; do not add new elements
- `GT` -- only update when the new score is **greater** than the current score
- `LT` -- only update when the new score is **less** than the current score
- `CH` -- modify return value from "number added" to "number changed" (added + updated)
- `INCR` -- act like ZINCRBY; increment the score instead of setting it (only one member allowed)

---

## 5. Hash Commands

Hashes map string fields to string values. Useful for representing objects (user profiles, sessions).

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **HSET** ⭐ | `HSET key field value [field value ...]` | O(1) per field | Set one or more field-value pairs in a hash (also replaces HMSET) |
| **HGET** ⭐ | `HGET key field` | O(1) | Get the value of a single field in a hash |
| **HMSET** | `HMSET key field value [field value ...]` | O(N) | **Deprecated** -- use HSET. Set multiple field-value pairs in a hash |
| **HMGET** | `HMGET key field [field ...]` | O(N) where N = fields | Get the values of multiple fields in a hash |
| **HDEL** | `HDEL key field [field ...]` | O(N) where N = fields | Delete one or more fields from a hash |
| **HEXISTS** | `HEXISTS key field` | O(1) | Check if a field exists in a hash |
| **HLEN** | `HLEN key` | O(1) | Get the number of fields in a hash |
| **HKEYS** | `HKEYS key` | O(N) | Get all field names in a hash |
| **HVALS** | `HVALS key` | O(N) | Get all values in a hash |
| **HGETALL** ⭐ | `HGETALL key` | O(N) | Get all field-value pairs in a hash |
| **HINCRBY** | `HINCRBY key field increment` | O(1) | Increment the integer value of a hash field by a given amount |
| **HINCRBYFLOAT** | `HINCRBYFLOAT key field increment` | O(1) | Increment the float value of a hash field by a given amount |
| **HSETNX** | `HSETNX key field value` | O(1) | Set a field's value only if the field does not already exist |
| **HRANDFIELD** | `HRANDFIELD key [count [WITHVALUES]]` | O(N) where N = count | Return one or more random fields from a hash |
| **HSCAN** | `HSCAN key cursor [MATCH pattern] [COUNT count]` | O(1) per call, O(N) full iteration | Incrementally iterate over fields and values of a hash |

---

## 6. Key Commands

Generic commands that operate on keys regardless of their value type.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **DEL** | `DEL key [key ...]` | O(N) for strings; O(M) for collections where M = elements | Synchronously delete one or more keys |
| **UNLINK** | `UNLINK key [key ...]` | O(1) per key; actual reclamation happens asynchronously | Asynchronously delete keys (non-blocking DEL) |
| **EXISTS** | `EXISTS key [key ...]` | O(N) where N = number of keys | Check if one or more keys exist; returns count of existing keys |
| **TYPE** | `TYPE key` | O(1) | Return the type of the value stored at a key (string, list, set, zset, hash, stream) |
| **RENAME** | `RENAME key newkey` | O(1) | Rename a key; overwrites newkey if it exists |
| **RENAMENX** | `RENAMENX key newkey` | O(1) | Rename a key only if the new key does not exist |
| **COPY** | `COPY source destination [DB db] [REPLACE]` | O(N) for nested collections | Copy a key's value to another key |
| **DUMP** | `DUMP key` | O(1) for access + O(N*M) for serialization | Serialize the value stored at a key in Redis-specific format |
| **RESTORE** | `RESTORE key ttl serialized-value [REPLACE] [ABSTTL] [IDLETIME sec] [FREQ freq]` | O(1) for access + O(N*M) for deserialization | Deserialize and store a value previously obtained with DUMP |
| **OBJECT** | `OBJECT HELP\|ENCODING\|FREQ\|IDLETIME\|REFCOUNT key` | O(1) | Inspect the internal encoding, reference count, idle time, or LFU frequency of a key |
| **SORT** | `SORT key [BY pattern] [LIMIT offset count] [GET pattern ...] [ASC\|DESC] [ALPHA] [STORE dest]` | O(N+M*log M) | Sort the elements in a list, set, or sorted set |
| **SORT_RO** | `SORT_RO key [BY pattern] [LIMIT offset count] [GET pattern ...] [ASC\|DESC] [ALPHA]` | O(N+M*log M) | Read-only variant of SORT (safe for replicas) |
| **TOUCH** | `TOUCH key [key ...]` | O(N) | Update the last access time of one or more keys (affects LRU/LFU) |
| **RANDOMKEY** | `RANDOMKEY` | O(1) | Return a random key from the current database |
| **SCAN** | `SCAN cursor [MATCH pattern] [COUNT count] [TYPE type]` | O(1) per call, O(N) full iteration | Incrementally iterate over the keyspace |
| **KEYS** | `KEYS pattern` | O(N) | Return all keys matching a pattern -- **DANGER: blocks on large databases; use SCAN instead** |
| **WAIT** ⭐ | `WAIT numreplicas timeout` | O(1) | Block until writes are acknowledged by N replicas or timeout elapses |
| **WAITAOF** | `WAITAOF numlocal numreplicas timeout` | O(1) | **7.2+**. Block until writes are fsynced to AOF on local and/or replicas |

---

## 7. Expiry Commands

Commands to set, query, and remove time-to-live on keys.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **EXPIRE** ⭐ | `EXPIRE key seconds [NX\|XX\|GT\|LT]` | O(1) | Set a timeout on a key in seconds |
| **PEXPIRE** | `PEXPIRE key milliseconds [NX\|XX\|GT\|LT]` | O(1) | Set a timeout on a key in milliseconds |
| **EXPIREAT** | `EXPIREAT key unix-time-seconds [NX\|XX\|GT\|LT]` | O(1) | Set expiry as an absolute UNIX timestamp in seconds |
| **PEXPIREAT** | `PEXPIREAT key unix-time-milliseconds [NX\|XX\|GT\|LT]` | O(1) | Set expiry as an absolute UNIX timestamp in milliseconds |
| **TTL** ⭐ | `TTL key` | O(1) | Get remaining time-to-live in seconds (-1 = no expiry, -2 = key missing) |
| **PTTL** | `PTTL key` | O(1) | Get remaining time-to-live in milliseconds |
| **PERSIST** | `PERSIST key` | O(1) | Remove the expiry from a key, making it persistent |
| **EXPIRETIME** | `EXPIRETIME key` | O(1) | **7.0+**. Get the absolute UNIX expiration timestamp in seconds |
| **PEXPIRETIME** | `PEXPIRETIME key` | O(1) | **7.0+**. Get the absolute UNIX expiration timestamp in milliseconds |

**Subcommand flags (6.2+) for EXPIRE/PEXPIRE/EXPIREAT/PEXPIREAT:**
- `NX` -- set expiry only if the key has no expiry
- `XX` -- set expiry only if the key already has an expiry
- `GT` -- set expiry only if the new expiry is greater than the current one
- `LT` -- set expiry only if the new expiry is less than the current one

---

## 8. HyperLogLog Commands

Probabilistic data structure for counting unique elements with constant memory (~12 KB per key). Standard error rate: 0.81%.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **PFADD** | `PFADD key element [element ...]` | O(1) per element | Add elements to a HyperLogLog |
| **PFCOUNT** | `PFCOUNT key [key ...]` | O(1) for single key, O(N) for multiple | Return the approximate cardinality (unique count) of a HyperLogLog |
| **PFMERGE** | `PFMERGE destkey sourcekey [sourcekey ...]` | O(N) where N = number of source keys | Merge multiple HyperLogLogs into one |

---

## 9. Bitmap Commands

Bitmaps are not a separate data type -- they are string values treated as arrays of bits. Useful for real-time analytics (daily active users, feature flags).

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **SETBIT** | `SETBIT key offset value` | O(1) | Set or clear the bit at a given offset in the string |
| **GETBIT** | `GETBIT key offset` | O(1) | Get the bit value at a given offset |
| **BITCOUNT** | `BITCOUNT key [start end [BYTE\|BIT]]` | O(N) | Count the number of set bits (1s) in a string |
| **BITOP** | `BITOP AND\|OR\|XOR\|NOT destkey key [key ...]` | O(N) | Perform bitwise operations between strings and store the result |
| **BITPOS** | `BITPOS key bit [start [end [BYTE\|BIT]]]` | O(N) | Find the first bit set to 0 or 1 in a string |
| **BITFIELD** | `BITFIELD key [GET enc offset] [SET enc offset value] [INCRBY enc offset incr] [OVERFLOW WRAP\|SAT\|FAIL]` | O(1) per sub-command | Perform arbitrary bitfield integer operations on strings |
| **BITFIELD_RO** | `BITFIELD_RO key [GET enc offset ...]` | O(1) per sub-command | Read-only variant of BITFIELD (safe for replicas) |

---

## 10. Geospatial Commands

Geospatial indexes use sorted sets under the hood, with scores being 52-bit geohash-encoded longitude/latitude.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **GEOADD** | `GEOADD key [NX\|XX] [CH] longitude latitude member [longitude latitude member ...]` | O(log N) per element | Add geospatial members (longitude, latitude, name) to a sorted set |
| **GEODIST** | `GEODIST key member1 member2 [M\|KM\|FT\|MI]` | O(1) | Return the distance between two members in the given unit |
| **GEOHASH** | `GEOHASH key member [member ...]` | O(N) | Return Geohash strings for one or more members |
| **GEOPOS** | `GEOPOS key member [member ...]` | O(N) | Return the longitude and latitude of one or more members |
| **GEOSEARCH** | `GEOSEARCH key FROMMEMBER member\|FROMLONLAT lon lat BYRADIUS radius M\|KM\|FT\|MI\|BYBOX w h M\|KM\|FT\|MI [ASC\|DESC] [COUNT count [ANY]] [WITHCOORD] [WITHDIST] [WITHHASH]` | O(N+log M) | **6.2+**. Search for members within a circle or rectangle (replaces GEORADIUS/GEORADIUSBYMEMBER) |
| **GEOSEARCHSTORE** | `GEOSEARCHSTORE destination source FROMMEMBER member\|FROMLONLAT lon lat BYRADIUS radius\|BYBOX w h unit [ASC\|DESC] [COUNT count [ANY]] [STOREDIST]` | O(N+log M) | Store the result of GEOSEARCH into a destination key |

---

## 11. Stream Commands

Streams are an append-only log data structure (Redis 5.0+). Conceptually similar to Apache Kafka -- supports consumer groups, acknowledgment, and pending message tracking.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **XADD** | `XADD key [NOMKSTREAM] [MAXLEN\|MINID [=\|~] threshold [LIMIT count]] *\|id field value [field value ...]` | O(1) for append; O(N) if trimming | Append a new entry to a stream |
| **XREAD** | `XREAD [COUNT count] [BLOCK ms] STREAMS key [key ...] id [id ...]` | O(N) where N = count | Read entries from one or more streams starting from a given ID |
| **XREADGROUP** | `XREADGROUP GROUP group consumer [COUNT count] [BLOCK ms] [NOACK] STREAMS key [key ...] id [id ...]` | O(M) where M = returned entries | Read entries from a stream as part of a consumer group |
| **XRANGE** | `XRANGE key start end [COUNT count]` | O(N) where N = returned entries | Return a range of entries in a stream by ID (ascending) |
| **XREVRANGE** | `XREVRANGE key end start [COUNT count]` | O(N) where N = returned entries | Return a range of entries in a stream by ID (descending) |
| **XLEN** | `XLEN key` | O(1) | Get the number of entries in a stream |
| **XTRIM** | `XTRIM key MAXLEN\|MINID [=\|~] threshold [LIMIT count]` | O(N) where N = evicted entries | Trim a stream to a given max length or minimum ID |
| **XDEL** | `XDEL key id [id ...]` | O(1) per entry | Delete one or more entries from a stream by ID |
| **XINFO** | `XINFO STREAM\|GROUPS\|CONSUMERS key [group] [FULL [COUNT count]]` | O(1) for STREAM/GROUPS; O(N) for CONSUMERS | Get information about a stream, its consumer groups, or consumers |
| **XGROUP** | `XGROUP CREATE\|SETID\|DESTROY\|CREATECONSUMER\|DELCONSUMER key group [id\|consumer] [MKSTREAM] [ENTRIESREAD n]` | O(1) | Manage consumer groups (create, destroy, set last-delivered ID) |
| **XACK** | `XACK key group id [id ...]` | O(1) per ID | Acknowledge one or more entries as processed by a consumer group member |
| **XPENDING** | `XPENDING key group [[IDLE min-idle-time] start end count [consumer]]` | O(N) where N = returned entries | Get information about pending entries (delivered but not acknowledged) |
| **XCLAIM** | `XCLAIM key group consumer min-idle-time id [id ...] [IDLE ms] [TIME ms] [RETRYCOUNT n] [FORCE] [JUSTID] [LASTID id]` | O(N) where N = claimed entries | Claim ownership of pending entries (transfer from one consumer to another) |
| **XAUTOCLAIM** | `XAUTOCLAIM key group consumer min-idle-time start [COUNT count] [JUSTID]` | O(1) | **6.2+**. Automatically claim pending entries that exceed the idle time threshold |

---

## 12. Pub/Sub Commands

Publish/Subscribe messaging. Messages are fire-and-forget -- no persistence, no acknowledgment.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **SUBSCRIBE** ⭐ | `SUBSCRIBE channel [channel ...]` | O(N) where N = channels | Subscribe to one or more channels |
| **UNSUBSCRIBE** | `UNSUBSCRIBE [channel [channel ...]]` | O(N) where N = channels | Unsubscribe from one or more channels (all if none specified) |
| **PUBLISH** ⭐ | `PUBLISH channel message` | O(N+M) N=subscribed clients, M=pattern subscribers | Publish a message to a channel |
| **PSUBSCRIBE** | `PSUBSCRIBE pattern [pattern ...]` | O(N) where N = patterns | Subscribe to channels matching glob-style patterns |
| **PUNSUBSCRIBE** | `PUNSUBSCRIBE [pattern [pattern ...]]` | O(N+M) | Unsubscribe from channels matching patterns |
| **PUBSUB** | `PUBSUB CHANNELS [pattern]\|NUMSUB [channel ...]\|NUMPAT\|SHARDCHANNELS [pattern]\|SHARDNUMSUB [channel ...]` | O(N) | Inspect the state of the Pub/Sub subsystem (list channels, subscriber counts) |
| **SSUBSCRIBE** | `SSUBSCRIBE shardchannel [shardchannel ...]` | O(N) | **7.0+**. Subscribe to shard channels (messages routed by hash slot in cluster) |
| **SUNSUBSCRIBE** | `SUNSUBSCRIBE [shardchannel [shardchannel ...]]` | O(N) | **7.0+**. Unsubscribe from shard channels |
| **SPUBLISH** | `SPUBLISH shardchannel message` | O(N) | **7.0+**. Publish a message to a shard channel (only delivered within the owning shard) |

---

## 13. Transaction Commands

Redis transactions provide atomic execution of a group of commands. Not ACID like SQL -- no rollback on individual command failure.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **MULTI** ⭐ | `MULTI` | O(1) | Start a transaction block; subsequent commands are queued |
| **EXEC** ⭐ | `EXEC` | O(N) where N = queued commands | Execute all queued commands in the transaction atomically |
| **DISCARD** | `DISCARD` | O(N) where N = queued commands | Discard all queued commands and exit the transaction |
| **WATCH** ⭐ | `WATCH key [key ...]` | O(1) per key | Optimistic locking -- mark keys to watch; EXEC fails if any watched key was modified |
| **UNWATCH** | `UNWATCH` | O(1) | Unwatch all previously watched keys |

**Transaction semantics:**
- Commands between MULTI and EXEC are queued, not executed immediately
- EXEC runs all queued commands atomically (serialized, no interleaving)
- If a WATCHed key changes before EXEC, the transaction is aborted (EXEC returns nil)
- There is NO rollback -- if one command in the transaction fails (e.g., wrong type), the rest still execute

---

## 14. Scripting & Functions

Lua scripting allows atomic multi-step operations. Redis 7.0+ added the Functions API for persistent, named functions.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **EVAL** ⭐ | `EVAL script numkeys [key ...] [arg ...]` | Depends on script | Execute a Lua script server-side with keys and arguments |
| **EVALSHA** | `EVALSHA sha1 numkeys [key ...] [arg ...]` | Depends on script | Execute a cached Lua script by its SHA1 digest |
| **EVALRO** | `EVALRO script numkeys [key ...] [arg ...]` | Depends on script | Execute a read-only Lua script (safe for replicas) |
| **EVALSHA_RO** | `EVALSHA_RO sha1 numkeys [key ...] [arg ...]` | Depends on script | Execute a cached read-only Lua script by SHA1 |
| **SCRIPT** | `SCRIPT EXISTS sha1 [sha1 ...]\|FLUSH [ASYNC\|SYNC]\|LOAD script\|DEBUG YES\|SYNC\|NO` | O(N) for EXISTS; O(1) otherwise | Manage the Lua script cache (check existence, flush, load, debug) |
| **FUNCTION** | `FUNCTION CREATE\|DELETE\|DUMP\|FLUSH\|LIST\|LOAD\|RESTORE\|STATS` | Varies | **7.0+**. Manage named, persistent server-side functions |
| **FCALL** | `FCALL function numkeys [key ...] [arg ...]` | Depends on function | **7.0+**. Call a server-side function by name |
| **FCALL_RO** | `FCALL_RO function numkeys [key ...] [arg ...]` | Depends on function | **7.0+**. Call a read-only server-side function (safe for replicas) |

---

## 15. Connection Commands

Commands for managing the client connection lifecycle.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **AUTH** | `AUTH [username] password` | O(N) where N = ACL rules | Authenticate to the server (pre-6.0: password only; 6.0+: username + password via ACL) |
| **PING** | `PING [message]` | O(1) | Test connectivity; returns PONG or echoes the given message |
| **ECHO** | `ECHO message` | O(1) | Echo the given string back (used for testing) |
| **QUIT** | `QUIT` | O(1) | Close the connection gracefully |
| **SELECT** | `SELECT index` | O(1) | Switch to a different database (0-15 by default; not supported in Cluster mode) |
| **HELLO** | `HELLO [protover [AUTH username password] [SETNAME clientname]]` | O(1) | **6.0+**. Switch protocol version (RESP2/RESP3), authenticate, and set client name in one call |
| **RESET** | `RESET` | O(1) | Reset the connection to its initial state (unwatch, unsubscribe, deauth) |
| **CLIENT** | `CLIENT CACHING\|GETNAME\|GETREDIR\|ID\|INFO\|KILL\|LIST\|NO-EVICT\|NO-TOUCH\|PAUSE\|REPLY\|SETNAME\|TRACKING\|TRACKINGINFO\|UNPAUSE\|UNBLOCK` | Varies | Manage client connections (list, kill, pause, set name, enable tracking) |

---

## 16. Server / Admin Commands

Commands for server management, monitoring, and operations.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **INFO** ⭐ | `INFO [section ...]` | O(1) | Return detailed server information and statistics (server, clients, memory, persistence, stats, replication, cpu, keyspace, etc.) |
| **DBSIZE** | `DBSIZE` | O(1) | Return the number of keys in the current database |
| **FLUSHDB** | `FLUSHDB [ASYNC\|SYNC]` | O(N) | Delete all keys in the current database |
| **FLUSHALL** | `FLUSHALL [ASYNC\|SYNC]` | O(N) | Delete all keys in all databases |
| **SAVE** | `SAVE` | O(N) | Synchronously save the dataset to disk (blocks the main thread -- avoid in production) |
| **BGSAVE** ⭐ | `BGSAVE [SCHEDULE]` | O(1) to fork | Trigger a background RDB save (fork + COW) |
| **BGREWRITEAOF** | `BGREWRITEAOF` | O(1) to fork | Trigger a background AOF rewrite |
| **LASTSAVE** | `LASTSAVE` | O(1) | Return the UNIX timestamp of the last successful RDB save |
| **CONFIG** | `CONFIG GET parameter [parameter ...]\|SET parameter value [parameter value ...]\|RESETSTAT\|REWRITE` | O(N) for GET with globs | Read or set server configuration parameters at runtime |
| **TIME** | `TIME` | O(1) | Return the server time as [unix-seconds, microseconds] |
| **SLOWLOG** ⭐ | `SLOWLOG GET [count]\|LEN\|RESET` | O(N) for GET | Manage the slow query log (default threshold: 10 ms) |
| **LATENCY** ⭐ | `LATENCY LATEST\|HISTORY event\|RESET [event ...]\|GRAPH event` | O(1) per event | Monitor latency spikes across multiple event types |
| **MEMORY** ⭐ | `MEMORY DOCTOR\|MALLOC-STATS\|PURGE\|STATS\|USAGE key [SAMPLES count]` | O(1) for most; O(N) for USAGE with samples | Inspect memory usage, run diagnostics, and purge allocator caches |
| **COMMAND** | `COMMAND [COUNT\|DOCS [cmd ...]\|GETKEYS cmd args\|INFO [cmd ...]\|LIST [FILTERBY MODULE name\|ACLCAT cat\|PATTERN pat]]` | O(N) | Introspect the server's command table (count, docs, key extraction) |
| **DEBUG** | `DEBUG OBJECT key\|SLEEP seconds\|SET-ACTIVE-EXPIRE 0\|1\|...` | Varies | Internal debugging commands (not for production use) |
| **MONITOR** | `MONITOR` | O(N) per command processed | Stream every command processed by the server in real time -- **DANGER: significant performance impact** |
| **SWAPDB** | `SWAPDB index1 index2` | O(N) where N = watching clients | Atomically swap two databases |
| **SHUTDOWN** | `SHUTDOWN [NOSAVE\|SAVE] [NOW] [FORCE]` | O(N) if saving | Stop the server, optionally saving the dataset |
| **FAILOVER** | `FAILOVER [TO host port [FORCE]] [ABORT] [TIMEOUT ms]` | O(1) | **6.2+**. Trigger a graceful failover to a replica |

---

## 17. ACL Commands (6.0+)

Access Control Lists for per-user command and key restrictions.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **ACL LIST** | `ACL LIST` | O(N) where N = users | List all ACL rules for all users |
| **ACL GETUSER** | `ACL GETUSER username` | O(N) where N = rules for the user | Get the ACL rules for a specific user |
| **ACL SETUSER** | `ACL SETUSER username [rule [rule ...]]` | O(N) where N = rules | Create or modify a user's ACL rules |
| **ACL DELUSER** | `ACL DELUSER username [username ...]` | O(N) where N = rules per user | Delete one or more users from the ACL |
| **ACL CAT** | `ACL CAT [categoryname]` | O(N) | List all ACL categories, or commands within a category |
| **ACL GENPASS** | `ACL GENPASS [bits]` | O(1) | Generate a secure random password (default 256-bit hex) |
| **ACL WHOAMI** | `ACL WHOAMI` | O(1) | Return the username of the current connection |
| **ACL LOG** | `ACL LOG [count\|RESET]` | O(N) | List recent ACL security events (auth failures, command denials) |
| **ACL SAVE** | `ACL SAVE` | O(N) | Save the current ACL configuration to the `aclfile` |
| **ACL LOAD** | `ACL LOAD` | O(N) | Reload the ACL configuration from the `aclfile` |
| **ACL DRYRUN** | `ACL DRYRUN username command [arg ...]` | O(1) | **7.0+**. Simulate whether a user can execute a command without running it |

---

## 18. Cluster Commands

Commands for managing Redis Cluster topology, slots, and node state.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **CLUSTER INFO** | `CLUSTER INFO` | O(1) | Return cluster state information (ok/fail, slots assigned, known nodes, etc.) |
| **CLUSTER NODES** | `CLUSTER NODES` | O(N) where N = nodes | Return the cluster configuration as seen by the current node |
| **CLUSTER SLOTS** ⭐ | `CLUSTER SLOTS` | O(N) | Return the mapping of hash slots to nodes (**deprecated 7.0+**; use CLUSTER SHARDS) |
| **CLUSTER SHARDS** | `CLUSTER SHARDS` | O(N) | **7.0+**. Return shard-centric view of the cluster (replacement for CLUSTER SLOTS) |
| **CLUSTER MYID** | `CLUSTER MYID` | O(1) | Return the node ID of the current node |
| **CLUSTER MEET** | `CLUSTER MEET ip port [cluster-bus-port]` | O(1) | Connect the current node to another node to form/join a cluster |
| **CLUSTER ADDSLOTS** | `CLUSTER ADDSLOTS slot [slot ...]` | O(N) where N = slots | Assign hash slots to the current node |
| **CLUSTER DELSLOTS** | `CLUSTER DELSLOTS slot [slot ...]` | O(N) where N = slots | Remove hash slot assignments from the current node |
| **CLUSTER SETSLOT** | `CLUSTER SETSLOT slot IMPORTING\|MIGRATING\|STABLE\|NODE node-id` | O(1) | Set the state of a hash slot (used during resharding) |
| **CLUSTER FAILOVER** | `CLUSTER FAILOVER [FORCE\|TAKEOVER]` | O(1) | Trigger a manual failover of the current node's leader |
| **CLUSTER RESET** | `CLUSTER RESET [HARD\|SOFT]` | O(N) | Reset the cluster configuration of the current node |
| **CLUSTER REPLICATE** | `CLUSTER REPLICATE node-id` | O(1) | Configure the current node as a replica of the specified node |
| **CLUSTER KEYSLOT** | `CLUSTER KEYSLOT key` | O(N) where N = key length | Return the hash slot for a given key |
| **CLUSTER COUNTKEYSINSLOT** | `CLUSTER COUNTKEYSINSLOT slot` | O(1) | Return the number of keys in a specific hash slot |
| **CLUSTER GETKEYSINSLOT** | `CLUSTER GETKEYSINSLOT slot count` | O(N) where N = count | Return the keys stored in a specific hash slot |
| **READONLY** | `READONLY` | O(1) | Enable read queries on a cluster replica (allows serving stale reads) |
| **READWRITE** | `READWRITE` | O(1) | Disable READONLY mode; replica rejects read queries again |
| **ASKING** | `ASKING` | O(1) | Flag the next command to be accepted by an IMPORTING node during slot migration |

---

## 19. Replication Commands

Commands for configuring and managing leader-follower replication.

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **REPLICAOF** ⭐ | `REPLICAOF host port\|NO ONE` | O(1) | Configure the current server as a replica of another, or promote to leader (`NO ONE`) |
| **PSYNC** | `PSYNC replicationid offset` | O(N) for full sync | **Internal**. Used by replicas to initiate full or partial resynchronization with the leader (not for client use) |

---

## 20. Module Commands (4.0+)

Commands for loading and managing Redis modules (e.g., RediSearch, RedisJSON, RedisTimeSeries).

| Command | Syntax | Time Complexity | Description |
|---------|--------|-----------------|-------------|
| **MODULE LOAD** | `MODULE LOAD path [arg ...]` | O(1) | Load a Redis module from a shared library (.so) file |
| **MODULE LOADEX** | `MODULE LOADEX path [CONFIG name value ...] [ARGS arg ...]` | O(1) | **7.0+**. Load a module with named configuration parameters |
| **MODULE UNLOAD** | `MODULE UNLOAD name` | O(1) | Unload a previously loaded module |
| **MODULE LIST** | `MODULE LIST` | O(N) where N = loaded modules | List all loaded modules with name, version, and path |

---

## Memcached API Comparison

Memcached is a focused, high-performance volatile cache. Its API reflects this simplicity -- roughly 15 commands versus Redis's 400+.

| Memcached Command | Description | Redis Equivalent(s) |
|-------------------|-------------|----------------------|
| `get <key>` | Retrieve a value by key | `GET key` |
| `gets <key>` | Retrieve a value with its CAS token (for optimistic locking) | `GET key` + `WATCH key` (different mechanism) |
| `set <key> <flags> <exptime> <bytes>` | Store a key-value pair (create or overwrite) | `SET key value EX seconds` |
| `add <key> <flags> <exptime> <bytes>` | Store only if the key does NOT exist | `SET key value NX EX seconds` |
| `replace <key> <flags> <exptime> <bytes>` | Store only if the key DOES exist | `SET key value XX EX seconds` |
| `append <key> <bytes>` | Append data to an existing value | `APPEND key value` |
| `prepend <key> <bytes>` | Prepend data to an existing value | No direct equivalent (use Lua script) |
| `cas <key> <flags> <exptime> <bytes> <cas_token>` | Compare-and-swap (store only if CAS token matches) | `WATCH key` + `MULTI` + `SET` + `EXEC` |
| `delete <key>` | Delete a key | `DEL key` |
| `incr <key> <value>` | Increment a numeric value | `INCRBY key value` |
| `decr <key> <value>` | Decrement a numeric value | `DECRBY key value` |
| `touch <key> <exptime>` | Update expiry without fetching the value | `EXPIRE key seconds` |
| `gat <key> <exptime>` | Get and touch (fetch + update expiry) | `GETEX key EX seconds` |
| `gats <key> <exptime>` | Get and touch with CAS token | `GETEX key EX seconds` + `WATCH` |
| `stats` | Server statistics | `INFO` |
| `flush_all [delay]` | Invalidate all keys (optionally with delay) | `FLUSHALL` |
| `version` | Server version string | `INFO server` |
| `quit` | Close connection | `QUIT` |

### Key Differences

| Dimension | Memcached (~15 commands) | Redis (400+ commands) |
|-----------|--------------------------|------------------------|
| **Data types** | Strings only (opaque byte blobs) | Strings, Lists, Sets, Sorted Sets, Hashes, Streams, HyperLogLog, Bitmaps, Geospatial |
| **Transactions** | CAS (compare-and-swap) only | MULTI/EXEC with WATCH (optimistic locking), Lua scripting |
| **Pub/Sub** | Not supported | SUBSCRIBE, PUBLISH, PSUBSCRIBE, Sharded Pub/Sub (7.0+) |
| **Scripting** | Not supported | Lua (EVAL/EVALSHA), Functions (7.0+) |
| **Persistence** | None -- volatile cache only | RDB snapshots, AOF log, Hybrid (RDB + AOF) |
| **Replication** | Not built-in | Async leader-follower, PSYNC2, Sentinel |
| **Clustering** | Client-side consistent hashing only | Server-side hash slots, gossip protocol, MOVED/ASK |
| **ACL / Auth** | SASL authentication | Per-user ACLs (6.0+) with command and key restrictions |
| **Philosophy** | Simple, focused, volatile cache | Data structure server -- "a database that happens to be in memory" |

---

## RESP Protocol

Redis uses the **RESP (REdis Serialization Protocol)** -- a text-based, human-readable wire protocol over TCP on **port 6379**.

### Why Text-Based?

Antirez (Redis creator) intentionally chose a text protocol over a binary one. You can literally `telnet localhost 6379` and type commands. This maximizes debuggability and ecosystem growth at the cost of marginal bandwidth overhead compared to binary protocols like Memcached's binary protocol.

### RESP2 Types (Redis 1.2+)

| Prefix | Type | Example | Description |
|--------|------|---------|-------------|
| `+` | Simple String | `+OK\r\n` | Non-binary-safe string, no newlines. Used for status replies. |
| `-` | Error | `-ERR unknown command\r\n` | Error message. First word is the error type (ERR, WRONGTYPE, etc.). |
| `:` | Integer | `:1000\r\n` | Signed 64-bit integer. Used for INCR, LLEN, EXISTS, etc. |
| `$` | Bulk String | `$5\r\nhello\r\n` | Binary-safe string with length prefix. `$-1\r\n` = null. |
| `*` | Array | `*2\r\n$3\r\nfoo\r\n$3\r\nbar\r\n` | Ordered list of other RESP types. `*-1\r\n` = null array. |

**Example -- SET command on the wire:**

```
Client sends (inline):    SET mykey hello
Client sends (multibulk): *3\r\n$3\r\nSET\r\n$5\r\nmykey\r\n$5\r\nhello\r\n
Server responds:          +OK\r\n
```

### RESP3 Additions (Redis 6.0+)

RESP3 adds richer types for more efficient client-side parsing, while remaining text-based and backward compatible. Clients opt in via the `HELLO 3` command.

| Prefix | Type | Example | Description |
|--------|------|---------|-------------|
| `%` | Map | `%2\r\n+key1\r\n:1\r\n+key2\r\n:2\r\n` | Key-value map (avoids clients having to pair up flat arrays from HGETALL) |
| `~` | Set | `~3\r\n+a\r\n+b\r\n+c\r\n` | Unordered set of elements (semantically richer than Array) |
| `#` | Boolean | `#t\r\n` or `#f\r\n` | True/false (previously conveyed as integer 1/0) |
| `=` | Verbatim String | `=15\r\ntxt:Some string\r\n` | Bulk string with an encoding hint (txt, mkd) -- used for human-readable content |
| `(` | Big Number | `(3492890328409238509324850943850943825024385\r\n` | Arbitrary precision integer |
| `_` | Null | `_\r\n` | Explicit null (replaces `$-1` bulk string null and `*-1` array null) |
| `,` | Double | `,1.23\r\n` | Floating-point number (previously returned as bulk string) |
| `>` | Push | `>3\r\n+message\r\n+channel\r\n+payload\r\n` | Out-of-band push data (Pub/Sub messages, client tracking invalidations) |

### RESP3 Benefits

- **Typed responses** -- clients no longer need to guess whether a Bulk String is a number, boolean, or actual string
- **Maps and Sets** -- HGETALL returns a Map instead of a flat array, eliminating client-side pairing
- **Push protocol** -- Pub/Sub and client-side caching invalidations are delivered as typed push messages, not mixed into the command response stream
- **Backward compatible** -- RESP2 clients continue to work; RESP3 is opt-in via `HELLO 3`

---

*This document is a companion to the [interview simulation](01-interview-simulation.md). Commands marked with ⭐ are the ones discussed during the interview.*