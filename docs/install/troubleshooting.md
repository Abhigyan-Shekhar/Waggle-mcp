# Troubleshooting

## `waggle-mcp: command not found`

Install with `pipx install waggle-mcp`, then run `pipx ensurepath` and restart your shell.

## Python or `pipx` issues

Use Python 3.11 or newer. If wheels fail to build, upgrade packaging tools first:

```bash
python3 -m pip install -U pip setuptools wheel
```

## Server exits immediately

Run:

```bash
waggle-mcp doctor
waggle-mcp serve --transport stdio
```

Look for invalid env vars, bad `WAGGLE_BACKEND` values, or an unwritable database path.

## Database path permissions

Set `WAGGLE_DB_PATH` to a writable location:

```bash
export WAGGLE_DB_PATH="$HOME/.waggle/waggle.db"
```

Then rerun `waggle-mcp doctor`.

## Embedding model download or local loading issues

The default local embedding model may download on first run. If you need an offline-safe startup path, set:

```bash
export WAGGLE_MODEL=deterministic
```

## Client cannot see tools

Confirm the client config points to:

```text
waggle-mcp serve --transport stdio
```

Then restart the client and verify the MCP entry is enabled.

If you run `waggle-mcp serve` in a terminal, you may also see a short startup banner; it is skipped automatically for stdio/non-TTY runs, and you can disable it with `--quiet` or `WAGGLE_BANNER=false`.

## Run Waggle diagnostics

```bash
waggle-mcp doctor
```

Use `waggle-mcp doctor --fix` if the doctor reports mixed embedding model IDs after a model change.

## Enable verbose logs

```bash
export WAGGLE_LOG_LEVEL=DEBUG
waggle-mcp serve --transport stdio
```

## Security and privacy

Waggle stores memory locally by default. If troubleshooting requires sharing logs, inspect them first so you do not leak transcript content or secrets.

---

## Windows-specific troubleshooting

### `waggle-mcp: command not found` on Windows

After installing with pipx, update your PATH:

```powershell
pipx ensurepath
```

Then **close and reopen** your terminal. If it still fails, add manually:

```powershell
$env:PATH += ";$env:USERPROFILE\.local\bin"
```

### PowerShell execution policy error

If you see `running scripts is disabled on this system`, run:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### UTF-8 stdout errors on Windows

```powershell
$env:PYTHONUTF8 = "1"
```

### Database path on Windows

```powershell
$env:WAGGLE_DB_PATH = "$env:APPDATA\waggle\waggle.db"
```

### Enable verbose logs on Windows

```powershell
$env:WAGGLE_LOG_LEVEL = "DEBUG"
waggle-mcp serve --transport stdio
```
