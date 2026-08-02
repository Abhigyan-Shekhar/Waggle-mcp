# Claude Self-Hosted Connector

This page documents the recommended Claude path for Waggle: users run Waggle on their own machine.

## Recommended Local Setup

For Claude Code and Claude Desktop, use Waggle as a local stdio MCP server. This has no hosted infrastructure cost and keeps memory on the user's machine.

```bash
pipx install waggle-mcp
waggle-mcp setup --yes --clients claude-code,claude-desktop
```

Manual Claude Code setup:

```bash
claude mcp add --transport stdio waggle -- waggle-mcp serve --transport stdio
```

Manual Claude Desktop config:

```json
{
  "mcpServers": {
    "waggle": {
      "command": "waggle-mcp",
      "args": ["serve", "--transport", "stdio"],
      "env": {
        "WAGGLE_DEFAULT_TENANT_ID": "local-default",
        "WAGGLE_DB_PATH": "~/.waggle/waggle.db"
      }
    }
  }
}
```

## Claude Web/Mobile Option

Claude web and mobile cannot launch a local stdio process on the user's computer. To use Waggle there, the user must expose their local Waggle HTTP server through a user-controlled HTTPS URL.

This is still self-hosted: Waggle runs on the user's machine, and the HTTPS URL points back to that machine through a tunnel or reverse proxy.

Print the setup commands for your machine:

```bash
waggle-mcp claude-self-host
```

The deprecated `--create-key` compatibility flag prints the same guide and points to the `create-api-key` command:

```bash
waggle-mcp claude-self-host --create-key
```

Provider examples:

```bash
waggle-mcp claude-self-host --tunnel-provider cloudflare --tunnel-url https://waggle.example.com
waggle-mcp claude-self-host --tunnel-provider ngrok --tunnel-url https://waggle.example.ngrok.app
waggle-mcp claude-self-host --tunnel-provider tailscale --tunnel-url https://your-device.tailnet.ts.net
```

Common options:

- Cloudflare Tunnel
- Tailscale Funnel
- ngrok
- localhost.run
- User-managed reverse proxy with TLS

Do not expose Waggle over plain HTTP or without authentication.

## Local HTTP Server

For single-user self-hosting, run HTTP mode with the default SQLite backend:

```bash
WAGGLE_TRANSPORT=http \
WAGGLE_BACKEND=sqlite \
WAGGLE_DEFAULT_TENANT_ID=local-default \
WAGGLE_DB_PATH=~/.waggle/waggle.db \
WAGGLE_HTTP_HOST=127.0.0.1 \
WAGGLE_HTTP_PORT=8080 \
WAGGLE_API_KEY_ENVIRONMENT=local \
waggle-mcp serve
```

Health checks:

```bash
curl http://127.0.0.1:8080/health/live
curl http://127.0.0.1:8080/health/ready
```

The local MCP URL before tunneling is:

```text
http://127.0.0.1:8080/mcp
```

After tunneling, Claude needs the public HTTPS URL:

```text
https://<user-owned-tunnel-domain>/mcp
```

## API Key

Create a local API key for the tunnel-exposed server:

```bash
WAGGLE_BACKEND=sqlite \
WAGGLE_DEFAULT_TENANT_ID=local-default \
WAGGLE_DB_PATH=~/.waggle/waggle.db \
waggle-mcp create-api-key \
  --tenant-id local-default \
  --name claude-self-hosted \
  --scopes graph:read,graph:write
```

The command prints `raw_api_key` exactly once. Store that value in your password manager or secret store before closing the terminal. Waggle stores only a verifier and cannot recover the raw key later.

Waggle accepts the generated key in either form:

```text
X-API-Key: <generated-key>
Authorization: Bearer <generated-key>
```

Use `Authorization: Bearer` for Claude's Messages API MCP connector, which exposes an `authorization_token` field. Use `X-API-Key` only with clients or Claude surfaces that support custom static headers.

## Tunnel Rules

Configure the tunnel to forward:

```text
https://<user-owned-tunnel-domain>/mcp -> http://127.0.0.1:8080/mcp
https://<user-owned-tunnel-domain>/health/live -> http://127.0.0.1:8080/health/live
https://<user-owned-tunnel-domain>/health/ready -> http://127.0.0.1:8080/health/ready
```

Security requirements:

- Keep the tunnel URL private when possible.
- Require `X-API-Key` for `/mcp`.
- Do not put API keys in URL query parameters.
- Rotate the API key if the tunnel URL or key is shared accidentally.
- Stop `waggle-mcp serve` or disable the tunnel when not using web/mobile.

## Add To Claude Web/Mobile

If the Claude plan/org supports custom remote MCP connectors:

1. Add a custom connector.
2. Set the server URL to `https://<user-owned-tunnel-domain>/mcp`.
3. Configure auth. Use `Authorization: Bearer <generated-key>` where bearer tokens are supported, or `X-API-Key: <generated-key>` where custom static headers are supported.
4. Connect and let Claude sync the tool list.
5. Test with: "Ask Waggle what context is remembered for this project."

If the Claude UI does not support bearer/static-header auth for the user's account, use Claude Code/Desktop locally or add an OAuth proxy in front of Waggle.

## Cost Model

Local stdio mode costs nothing beyond the user's computer.

Self-hosted web/mobile can be free or low-cost depending on:

- The tunnel provider.
- Whether the user leaves the tunnel running continuously.

For the lowest cost and lowest risk, prefer local stdio. Use the HTTPS tunnel only when Claude web/mobile access is necessary.

## Team Or Production Backend

SQLite is the simplest self-hosted path for one user. Use Neo4j when you need stronger multi-tenant or team-oriented deployment characteristics:

```bash
WAGGLE_TRANSPORT=http \
WAGGLE_BACKEND=neo4j \
WAGGLE_DEFAULT_TENANT_ID=workspace-default \
WAGGLE_NEO4J_URI=bolt://localhost:7687 \
WAGGLE_NEO4J_USERNAME=neo4j \
WAGGLE_NEO4J_PASSWORD=change-me \
WAGGLE_HTTP_HOST=127.0.0.1 \
WAGGLE_HTTP_PORT=8080 \
WAGGLE_API_KEY_ENVIRONMENT=local \
waggle-mcp serve
```
