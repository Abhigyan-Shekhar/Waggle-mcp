# Troubleshooting

## `waggle-mcp: command not found`

The package may already be installed while pipx's app directory is missing from
your shell's `PATH`. On zsh, repair and verify it with:

```bash
pipx ensurepath
exec zsh -l
command -v waggle-mcp
waggle-mcp doctor
```

For another shell, close and reopen the terminal instead of running
`exec zsh -l`. If `pipx list` shows `waggle-mcp` but `command -v waggle-mcp`
is still empty, run `pipx reinstall waggle-mcp` to recreate its app link.
Updating pipx itself is not normally required for this error.

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

## `AuthenticationError` over HTTP transport

Requests to `/mcp` require a valid API key header. The health and metrics endpoints (`/health/live`, `/health/ready`, `/metrics`) do not.

Three distinct messages can surface here:

- `Missing X-API-Key header.` — no `X-API-Key` header was sent.
- `Invalid API key.` — a key was sent, but it is unknown, not active, or the hash did not verify.
- `API key expired.` — the key was found but its `expires_at` has already passed.

In all three cases, send a valid `X-API-Key: <your_key>` with the request. Issue one with:

```bash
waggle-mcp create-api-key --tenant-id <tenant> --name <label> --scopes "graph:read,graph:write"
```

See [docs/reference.md](../reference.md) for the full admin command list and [docs/security/security-model.md](../security/security-model.md#authentication) for how API keys and tenant scopes work.

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

### Quoting paths with spaces in MCP client configs

MCP clients read JSON config files literally. Shell quoting rules from PowerShell or
`cmd.exe` do not apply inside `claude_desktop_config.json`, `mcp.json`, or
`.vscode/mcp.json`. This matters when Python, your project folder, or
`WAGGLE_DB_PATH` contains spaces (for example `C:\Program Files\...` or
`C:\E Drive\in progress 2\...`).

**Rules for JSON config files:**

- Put the executable in `"command"` and flags in `"args"`; do not join them into one shell string.
- Escape backslashes in JSON strings by doubling them: `\\`.
- Forward slashes also work on Windows and are often easier to read in JSON:
  `C:/Users/Amir/AppData/Roaming/waggle/waggle.db`.
- Do not rely on `~` in JSON on Windows; use a full path or set `WAGGLE_DB_PATH`
  through your shell before launching the client.

**Example: local venv under a path with spaces**

```json
{
  "mcpServers": {
    "waggle": {
      "command": "C:/E Drive/in progress 2/Waggle-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "waggle.server", "serve", "--transport", "stdio"],
      "env": {
        "WAGGLE_BACKEND": "sqlite",
        "WAGGLE_DB_PATH": "C:/Users/Amir/AppData/Roaming/waggle/waggle.db",
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

**Test the command outside the client first**

PowerShell:

```powershell
& "C:\E Drive\in progress 2\Waggle-mcp\.venv\Scripts\python.exe" -m waggle.server serve --transport stdio
```

`cmd.exe`:

```cmd
"C:\E Drive\in progress 2\Waggle-mcp\.venv\Scripts\python.exe" -m waggle.server serve --transport stdio
```

If the manual command starts but the MCP client fails, compare your JSON `command`
and `args` against the working shell invocation. A missing quote or single backslash
in JSON is the most common cause.

When `waggle-mcp` is already on `PATH` (for example after `pipx install waggle-mcp`),
the simple config in [generic-mcp.md](./generic-mcp.md) is usually enough because the
command name has no spaces.

### Enable verbose logs on Windows

```powershell
$env:WAGGLE_LOG_LEVEL = "DEBUG"
waggle-mcp serve --transport stdio
```

---

## Codex plugin binary blocked by OS

The bundled Waggle runtime shipped with the Codex plugin is intentionally
unsigned for the current self-hosted marketplace bundle. macOS and Windows can
block it on first run. This is expected behavior, not a broken install.

### macOS — approve and retry

1. Open **System Settings → Privacy & Security**
2. Find the blocked binary entry and click **Allow Anyway**
3. Restart Codex

Or remove the quarantine flag manually:

```bash
xattr -dr com.apple.quarantine /path/to/waggle-runtime
```

### Windows — approve and retry

1. When SmartScreen appears, click **More info**
2. Click **Run anyway**
3. Restart Codex

If the plugin still fails after approval, run:

```bash
waggle-mcp doctor
```
