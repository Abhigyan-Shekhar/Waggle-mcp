# Smithery

Use this when you want Waggle discoverable from Smithery while keeping the runtime local-first over stdio.

Waggle is local graph memory for coding agents.

No cloud account. No API key. Local by default.

## One-line install

Install Waggle first:

```bash
pipx install waggle-mcp
pipx ensurepath
```

Restart the terminal after the first `pipx ensurepath` before starting Waggle.

## Manual config

The repo includes a root `smithery.yaml` that starts Waggle as a local stdio server:

```yaml
startCommand:
  type: stdio
  commandFunction: |-
    (config) => ({
      command: "waggle-mcp",
      args: ["serve", "--transport", "stdio"]
    })
```

## Validate

Install the current Smithery CLI and log in:

```bash
npm install -g @smithery/cli@latest
smithery auth login
```

Then validate the local CLI surface before publishing:

```bash
waggle-mcp doctor
waggle-mcp serve --transport stdio
```

## Publish

Current Smithery docs describe this stdio publishing command:

```bash
smithery mcp publish --name @your-org/waggle-mcp --transport stdio
```

If you later publish a hosted HTTP endpoint instead, Smithery’s current URL-based command is:

```bash
smithery mcp publish "https://your-server.com/mcp" -n @your-org/waggle-mcp
```

## Verify

```bash
smithery --help
```

## Troubleshooting

See [troubleshooting.md](./troubleshooting.md).

## Security and privacy

Smithery metadata should point to a local stdio launch path. Keep Waggle on stdio by default so users retain local storage and process-level control.
