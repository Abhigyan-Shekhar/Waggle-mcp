# Performance Diagnostics

## SQLite query plan logging

Waggle MCP can log SQLite query plans for hot SQL paths using the `WAGGLE_LOG_QUERY_PLANS` environment variable.

- Default: `false`
- Enable by setting: `WAGGLE_LOG_QUERY_PLANS=true`

When enabled, Waggle logs an `EXPLAIN QUERY PLAN` result at `DEBUG` level for selected hot-path queries.

## What is logged

The logging helper prepends `EXPLAIN QUERY PLAN` to the SQL statement and logs the executed plan along with:

- the original SQL statement
- the bound parameters
- the supplied query label

Example labels in the codebase include:

- `transcript list`
- `transcript count`
- `edge expansion`
- `node similarity`
- `conflict lookup`

## Understanding SCAN vs SEARCH

SQLite query plans usually report either `SCAN` or `SEARCH` for table access.

- `SCAN` means SQLite is scanning the entire table.
- `SEARCH` means SQLite is using an index.

A `SCAN` on a large table often indicates a missing or unused index for the query filter.

## Identifying missing indexes

If the logged plan shows `SCAN TABLE <table>` for a query that filters on one or more columns, those columns are candidates for indexing.

A good next step is to inspect the schema and existing indexes with SQLite:

```sql
PRAGMA index_list(<table>);
PRAGMA index_info(<index_name>);
```

Then add an index covering the filter or join columns used by the hot query.

## Notes

- Logs are only emitted when `WAGGLE_LOG_QUERY_PLANS=true`.
- Query plans are written at `DEBUG` level, so the logger must be configured to show debug output.
- This feature is intentionally lightweight when disabled; no explain queries are executed unless the environment variable is enabled.
