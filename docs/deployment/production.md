# Production Deployment

This guide describes the recommended self-hosted production shape for `waggle-mcp` when evaluating or piloting Waggle inside a company.

## Overview

Recommended baseline:

- HTTPS terminated at a reverse proxy
- `waggle-mcp` running in HTTP mode
- Neo4j running on a private network only
- API-key authentication enabled for all remote clients
- persistent volumes for Neo4j data and Waggle exports

Architecture:

```text
Client / MCP consumer
        |
        | HTTPS
        v
Reverse proxy (Caddy or Nginx)
        |
        | internal HTTP
        v
waggle-mcp application service
        |
        | Bolt
        v
Neo4j
```

Optional:

```text
waggle-mcp
    |
    v
audit log sink / file / database
```

## Required services

- `waggle-mcp`
- `neo4j`
- one reverse proxy:
  - `caddy`
  - `nginx`

## Environment variables

Core application settings:

```env
WAGGLE_TRANSPORT=http
WAGGLE_BACKEND=neo4j
WAGGLE_HTTP_HOST=0.0.0.0
WAGGLE_HTTP_PORT=8080
WAGGLE_DEFAULT_TENANT_ID=workspace-default
WAGGLE_API_KEY_ENVIRONMENT=live
WAGGLE_LOG_LEVEL=INFO
WAGGLE_RATE_LIMIT_RPM=120
WAGGLE_WRITE_RATE_LIMIT_RPM=60
WAGGLE_MAX_CONCURRENT_REQUESTS=8
WAGGLE_MAX_PAYLOAD_BYTES=1048576
WAGGLE_REQUEST_TIMEOUT_SECONDS=30
```

Neo4j settings:

```env
WAGGLE_NEO4J_URI=bolt://neo4j:7687
WAGGLE_NEO4J_USERNAME=neo4j
WAGGLE_NEO4J_PASSWORD=change_me
WAGGLE_NEO4J_DATABASE=neo4j
```

Recommended deployment-level variables:

```env
WAGGLE_DOMAIN=waggle.example.com
NEO4J_AUTH=neo4j/change_me
WAGGLE_RETENTION_ENABLED=true
WAGGLE_RETENTION_DAYS=90
WAGGLE_RETENTION_PRUNE_INTERVAL_HOURS=24
```

## Docker Compose baseline

Use [docker-compose.prod.yml](../../docker-compose.prod.yml) as the starting point.

Bring the stack up:

```bash
docker compose -f docker-compose.prod.yml up -d
```

That compose file assumes:

- `waggle-mcp` listens on internal port `8080`
- Caddy terminates HTTPS and proxies to `waggle:8080`
- Neo4j is reachable only on the internal compose network

## Reverse proxy

### Caddy

Recommended for the first production baseline because certificate management is straightforward.

Example file:

- [examples/Caddyfile](../../examples/Caddyfile)

Key points:

- set `WAGGLE_DOMAIN`
- point DNS at the host
- expose only `80/443`
- do not expose `8080` or `7687` publicly

### Nginx

Alternative if the buyer already standardizes on Nginx.

Example file:

- [examples/nginx.conf](../../examples/nginx.conf)

Key points:

- terminate TLS at Nginx
- forward `Host`, `X-Real-IP`, `X-Forwarded-For`, and `X-Forwarded-Proto`
- restrict the upstream app and database to private networking

## Neo4j production notes

Minimum production expectations:

- enable authentication
- use a strong password
- persist `/data` and `/logs`
- restrict Bolt (`7687`) to private network access
- do not publish Neo4j directly to the internet

Operational notes:

- set `WAGGLE_NEO4J_URI` to the internal service hostname
- size Neo4j memory explicitly on real deployments
- monitor disk space and database growth
- validate backup and restore before production use

## API keys

Today, remote access relies on Waggle API keys and tenant isolation.

Set `WAGGLE_API_KEY_ENVIRONMENT=live` before issuing production keys so generated keys use the `sk_live_` prefix. Local and default installs use the non-production `sk_test_` prefix unless this variable is explicitly set to `live`.

Recommended operational flow:

1. Create a tenant:

```bash
waggle-mcp create-tenant --tenant-id workspace-a --name "Workspace A"
```

2. Create a key:

```bash
waggle-mcp create-api-key --tenant-id workspace-a --name "prod-agent" \
  --expires-in-days 30 --created-by "ops@example.com" \
  --scopes "graph:read,graph:write,admin:read"
```

3. Store the issued key in your secret manager.
4. Configure clients to send it in `X-API-Key`.
5. Rotate by issuing a new key, updating clients, then revoking the old key.

Available admin metadata:

- public key prefix
- expiration timestamp
- created-by tag
- last-used timestamp on the stored record
- scopes

Recommended scope split:

- agent/runtime keys: `graph:read,graph:write`
- operator read-only keys: `admin:read`
- operator mutation keys: `admin:read,admin:write`
- full local admin keys: `graph:read,graph:write,admin:read,admin:write`

## Retention and pruning

Core supports a per-tenant retention policy and manual prune runs.

Recommended baseline:

```bash
waggle-mcp set-retention --tenant-id workspace-a --enabled --days 90 --interval-hours 24
waggle-mcp retention-status --tenant-id workspace-a
```

For now, schedule prune execution externally:

```bash
waggle-mcp prune-retention --tenant-id workspace-a
```

Each run stores a prune summary that can be reviewed with:

```bash
waggle-mcp list-retention-runs --tenant-id workspace-a --limit 20
```

HTTP admin surface for self-hosted deployments:

```text
GET   /api/admin/retention
PUT   /api/admin/retention
POST  /api/admin/retention/prune
GET   /api/admin/retention/runs
GET   /api/admin/audit-events
```

These endpoints can scope by `X-API-Key` or explicit `tenant_id`.
When `X-API-Key` is present, the admin endpoints enforce `admin:read` or `admin:write` depending on the route.

## Audit events

Core exposes recent audit events over both CLI and HTTP:

```bash
waggle-mcp list-audit-events --tenant-id workspace-a --type api_key.created --limit 50
```

```text
GET /api/admin/audit-events?tenant_id=workspace-a&type=api_key.created&limit=50
```

## Backups

At minimum, back up:

- Neo4j database data
- Waggle export directory
- deployment configuration
- reverse-proxy configuration

Recommended practice:

1. schedule regular Neo4j backups
2. keep encrypted off-host copies
3. test restore into a staging environment
4. document restore ownership and RTO/RPO expectations

## Upgrades

Recommended upgrade flow:

1. back up Neo4j
2. record the current container tags and env config
3. deploy the new Waggle version into staging
4. run smoke checks against `/live`, `/ready`, and `/mcp`
5. promote to production during a defined window

## Monitoring

At minimum, monitor:

- reverse-proxy availability
- Waggle `/live` and `/ready`
- Neo4j health
- error logs
- rate-limit spikes
- export activity
- backup success/failure

## Troubleshooting

Common issues:

- `HTTP transport requires WAGGLE_BACKEND=neo4j`
  - set `WAGGLE_BACKEND=neo4j`
- `Missing X-API-Key header`
  - ensure the client sends `X-API-Key`
- reverse proxy returns `502`
  - confirm Waggle is listening on `8080` internally
- Neo4j connection failures
  - verify `WAGGLE_NEO4J_URI`, auth, and network policy

## Security notes

- Do not expose Waggle publicly without authentication.
- Do not expose Neo4j directly to the public internet.
- TLS termination, firewalling, backups, and host-level security remain the responsibility of the self-hosting operator.
- See [security model](../security/security-model.md) and [hardening checklist](../security/hardening-checklist.md).
