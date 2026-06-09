# Graph Concurrency Protocol

## Overview

The SQLite-backed graph store uses three layers of concurrency control:

1. SQLite WAL mode (`PRAGMA journal_mode=WAL`)
2. An in-process `_ReadWriteLock`
3. A cross-process `ProcessLock`

These mechanisms are complementary and serve different purposes.

| Layer            | Scope                 | Purpose                                                                  |
| ---------------- | --------------------- | ------------------------------------------------------------------------ |
| SQLite WAL       | Database-wide         | Allows concurrent readers while a writer is active                       |
| `_ReadWriteLock` | Single Python process | Coordinates threads inside one process                                   |
| `ProcessLock`    | Multiple processes    | Prevents concurrent multi-statement operations against the same database |

## SQLite WAL Mode

Connections created by `_connect()` enable WAL mode and configure a busy timeout.

WAL mode allows readers to continue while a writer is active and significantly reduces reader/writer contention compared to rollback journaling.

The connection timeout is configured as:

```text
busy_timeout = 30000 ms
```

This means SQLite may wait up to approximately 30 seconds before reporting a locking failure when contention occurs.

## `_ReadWriteLock`

`_ReadWriteLock` coordinates access between threads within a single Python process.

Operations that modify graph state typically acquire the write lock before opening a database connection.

The lock does not coordinate separate OS processes.

## `ProcessLock`

`ProcessLock` provides cross-process coordination for operations that execute multiple SQL statements and must be treated as a single logical unit.

Use `ProcessLock` when an operation:

* Performs schema migration.
* Updates large batches of rows.
* Executes a multi-step transaction whose intermediate state should not be observed by another process.
* Persists and enriches conversation data across multiple statements.

## Lock Acquisition Order

To avoid deadlocks, acquire locks in the following order:

1. `ProcessLock`
2. `_ReadWriteLock`
3. SQLite connection (`_connect()`)

New code should follow this ordering consistently.

## Methods Using ProcessLock

| Method                        | Reason                                                                                        |
| ----------------------------- | --------------------------------------------------------------------------------------------- |
| `_initialize_database()`      | Protect schema creation, migrations, index creation, tenant bootstrap, and WAL initialization |
| `reembed_stale_embeddings()`  | Protect multi-row embedding refresh operations                                                |
| `observe_conversation()`      | Protect persistence and enrichment of a conversation turn                                     |
| `ingest_transcript_handoff()` | Protect batch transcript ingestion and associated writes                                      |

## Stale Lock Recovery

`ProcessLock` is implemented using a lock file.

If a process exits unexpectedly while holding the lock, subsequent lock acquisition attempts recover the stale lock according to the implementation in `locks.py`.

Contributors should not manually delete lock files while a process may still be active.

## Guidance for New Contributors

Before adding a new background task or maintenance job:

1. Determine whether the operation is a single SQL statement or a multi-statement workflow.
2. Use WAL alone for ordinary database access.
3. Use `_ReadWriteLock` for thread safety inside a process.
4. Use `ProcessLock` for multi-step operations that must be serialized across processes.
5. Follow the documented lock acquisition order.
