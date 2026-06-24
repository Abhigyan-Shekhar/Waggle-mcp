# waggle-mcp doctor --json Output Reference

## Overview

The `waggle-mcp doctor --json` command provides machine-readable diagnostic information about the current Waggle MCP installation.

This document explains the JSON response structure, status values, common failure scenarios, and recommended troubleshooting actions.

## Output Schema

## JSON Fields

| Field     | Type   | Description                                                      |
| --------- | ------ | ---------------------------------------------------------------- |
| `version` | string | Installed `waggle-mcp` package version.                          |
| `checks`  | object | Collection of diagnostic checks performed by the doctor command. |
| `summary` | object | Summary count of checks grouped by status.                       |

### Checks Object

Each entry inside `checks` contains:

| Field      | Type   | Description                                           |
| ---------- | ------ | ----------------------------------------------------- |
| `status`   | string | Result of the check (`ok`, `warn`, or `fail`).        |
| `reason`   | string | Explanation when a check fails or produces a warning. |
| `path`     | string | File or database path related to the check.           |
| `model_id` | string | Embedding model identifier when applicable.           |
| `mode`     | string | Startup mode currently configured.                    |
| `found_in` | string | Configuration file where the setting was detected.    |

### Summary Object

| Field  | Type    | Description                  |
| ------ | ------- | ---------------------------- |
| `ok`   | integer | Number of successful checks. |
| `warn` | integer | Number of warning checks.    |
| `fail` | integer | Number of failed checks.     |

## Example: Healthy Installation

A healthy installation indicates that all diagnostic checks completed successfully.

```json
{
  "version": "0.0.1",
  "checks": {
    "db_connection": {
      "status": "ok",
      "path": "/home/user/.waggle/waggle.db"
    },
    "embedding_model": {
      "status": "ok",
      "model_id": "deterministic"
    },
    "graph_schema": {
      "status": "ok"
    },
    "mcp_config": {
      "status": "ok"
    },
    "startup_mode": {
      "status": "ok",
      "mode": "normal"
    },
    "stdout_encoding": {
      "status": "ok"
    }
  },
  "summary": {
    "ok": 6,
    "warn": 0,
    "fail": 0
  }
}
```

### Recommended Action

No action is required. The installation appears to be functioning normally.
## Example: Missing Dependencies

This example shows a failed dependency check caused by a missing embedding model.

```json id="lx38vh"
{
  "version": "0.0.1",
  "checks": {
    "db_connection": {
      "status": "ok"
    },
    "embedding_model": {
      "status": "fail",
      "reason": "Embedding model not found locally."
    },
    "graph_schema": {
      "status": "ok"
    }
  },
  "summary": {
    "ok": 2,
    "warn": 0,
    "fail": 1
  }
}
```

### Troubleshooting

* Verify that the required embedding model is installed.
* Re-run setup if the model was not downloaded correctly.
* Check internet connectivity if model downloads are performed automatically.
## Example: Invalid Configuration

This example shows a configuration problem where no valid Waggle MCP server configuration could be found.

```json id="ptk26d"
{
  "version": "0.0.1",
  "checks": {
    "mcp_config": {
      "status": "fail",
      "reason": "No MCP client config file contains a 'waggle' server entry."
    },
    "db_connection": {
      "status": "ok"
    },
    "embedding_model": {
      "status": "ok"
    }
  },
  "summary": {
    "ok": 2,
    "warn": 0,
    "fail": 1
  }
}
```

### Troubleshooting

* Verify that your MCP client configuration contains a `waggle` server entry.
* Check configuration file syntax for JSON or TOML errors.
* Restart the MCP client after updating the configuration.
## Example: Model Download Failure

This example shows a failure while downloading or initializing the configured embedding model.

```json
{
  "version": "0.0.1",
  "checks": {
    "embedding_model": {
      "status": "fail",
      "reason": "Failed to download embedding model."
    },
    "db_connection": {
      "status": "ok"
    },
    "graph_schema": {
      "status": "ok"
    }
  },
  "summary": {
    "ok": 2,
    "warn": 0,
    "fail": 1
  }
}
```

### Troubleshooting

* Verify internet connectivity.
* Check whether the model source is reachable.
* Retry the setup process.
* Confirm that the configured model identifier is valid.

## Status Reference

| Status | Meaning                       | Recommended Action                                        |
| ------ | ----------------------------- | --------------------------------------------------------- |
| `ok`   | Check completed successfully. | No action required.                                       |
| `warn` | Potential issue detected.     | Review warning details and configuration.                 |
| `fail` | Check failed.                 | Follow the troubleshooting guidance for the failed check. |

## Diagnostic Checks

| Check             | Description                                                               |
| ----------------- | ------------------------------------------------------------------------- |
| `mcp_config`      | Verifies that an MCP client configuration contains a Waggle server entry. |
| `db_connection`   | Verifies database accessibility and configuration.                        |
| `embedding_model` | Verifies embedding model availability and initialization.                 |
| `graph_schema`    | Checks for graph schema consistency.                                      |
| `startup_mode`    | Reports the configured startup mode.                                      |
| `stdout_encoding` | Verifies UTF-8 compatible output encoding.                                |

## Troubleshooting Guide

| Check             | Common Cause                          | Recommended Resolution                                                   |
| ----------------- | ------------------------------------- | ------------------------------------------------------------------------ |
| `mcp_config`      | Missing or invalid MCP configuration  | Verify the MCP configuration file and add a valid `waggle` server entry. |
| `db_connection`   | Database path missing or inaccessible | Verify the configured database path and permissions.                     |
| `embedding_model` | Model missing or failed download      | Re-run setup and verify network connectivity.                            |
| `graph_schema`    | Inconsistent stored metadata          | Rebuild or repair the graph storage.                                     |
| `stdout_encoding` | Unsupported terminal encoding         | Configure the terminal to use UTF-8 encoding.                            |
