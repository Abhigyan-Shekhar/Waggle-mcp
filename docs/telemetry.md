# Telemetry and Local Logging

Waggle is local-first and does not send telemetry by default. Local operation keeps memory data on the user's machine unless the user explicitly exports, syncs, or shares it.

## Telemetry opt-out

No telemetry is enabled by default, so there is no telemetry service to opt out of for normal local operation.

To minimize application log output, set the logging level to `ERROR` before starting Waggle:

```bash
export WAGGLE_LOG_LEVEL=ERROR


``
This controls the verbosity of Waggle's local application logs. It does not disable local memory storage or delete existing logs or database data.
## What remains available locally
Reducing application logging does not disable Waggle's local memory database. Memory data continues to be stored locally using the configured WAGGLE_DB_PATH, which defaults to ~/.waggle/waggle.db.
Local application logs may still contain operational information needed for debugging and health checks, depending on the configured log level.
For more information about local-first storage and privacy, see the project's security documentation.
``
