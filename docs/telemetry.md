# Anonymous Telemetry

Waggle is local-first and does not enable telemetry by default.

Anonymous telemetry is optional. It exists to measure whether Waggle is actually
helping developers install, activate, and reuse local project memory. It is not
used to collect conversations, prompts, memory text, source code, file paths,
repository names, project names, tenant names, or stack traces.

## Status

Telemetry defaults to disabled.

Check the local setting:

```bash
waggle-mcp telemetry status
```

Enable it explicitly:

```bash
waggle-mcp telemetry enable
```

Disable it:

```bash
waggle-mcp telemetry disable
```

During setup, you can opt in or opt out explicitly:

```bash
waggle-mcp setup --yes --telemetry
waggle-mcp setup --yes --no-telemetry
```

Interactive setup asks the same question and defaults to No:

```bash
waggle-mcp init
```

Environment variables override the local setting for the current process:

```bash
WAGGLE_TELEMETRY=1
WAGGLE_TELEMETRY=0
```

## What Is Counted

Waggle measures anonymous active installations, not exact users.

An active installation is a local Waggle installation with a random UUID that
successfully performs a meaningful memory operation during a time window.

Meaningful memory operations include:

- `memory_stored`
- `memory_retrieved`
- `context_primed`
- `demo_completed`
- `export_completed`

These do not count as active memory usage:

- `waggle-mcp --help`
- installation alone
- `doctor` alone
- failed startup attempts
- CI test execution

Because telemetry is opt-in, active-installation counts are lower bounds.

## Installation Identity

When telemetry is enabled, Waggle creates a random installation UUID and stores
it in:

```text
~/.waggle/telemetry.json
```

The UUID is random. It is not derived from:

- IP address
- MAC address
- hostname
- username
- GitHub account
- repository path
- machine hardware
- project content

One person using Waggle on two machines counts as two installations.

## Event Queue

Telemetry events are queued locally before delivery:

```text
~/.waggle/telemetry-queue.jsonl
```

Queue limits:

- maximum 100 events
- maximum 7-day retention
- maximum 20 events per flush
- sub-second HTTP timeout

Telemetry must never block or break Waggle. Delivery failures are ignored.

## Allowed Events

The client only accepts this small event set:

- `setup_completed`
- `server_started`
- `memory_stored`
- `memory_retrieved`
- `context_primed`
- `demo_completed`
- `export_completed`
- `operation_failed`

Unknown events are rejected before they are queued.

## Allowed Properties

Telemetry properties are allowlisted:

- `waggle_version`
- `python_version`
- `os`
- `architecture`
- `client`
- `transport`
- `backend`
- `embedding_mode`
- `success`
- `duration_bucket`
- `result_count_bucket`
- `error_category`

Waggle prefers buckets over exact values, such as `1-5` results or
`100-500ms` duration.

## Never Collected

The telemetry layer strips forbidden or unknown fields. These values must not be
sent:

- query text
- prompts
- conversations
- memory text
- node content
- transcript text
- source code
- file paths
- repository names
- project names
- tenant names
- raw exception messages
- stack traces

Use this command to print an example of the exact sanitized payload shape:

```bash
waggle-mcp telemetry show
```

The example intentionally includes forbidden fields before sanitization, and the
printed payload shows that they are removed.

## Endpoint

The current client endpoint is:

```text
https://analytics.waggle.dev/v1/events
```

The endpoint should validate event names, delete unknown properties, rate-limit
abuse, and forward only sanitized events to the analytics backend.

Do not embed provider-specific analytics keys in the local Waggle runtime.
