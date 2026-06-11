# Security Model

## Overview

`waggle-mcp` is a self-hosted memory system for MCP-compatible clients. It stores transcript provenance, extracted graph data, and optional export artifacts.

This document explains the current trust boundaries and operational security model for Waggle Core. It is intended for engineering reviews and enterprise evaluation, not as a guarantee that every deployment is hardened by default.

## Trust boundaries

Primary boundaries:

- client or browser
- reverse proxy
- Waggle application service
- Neo4j or local storage backend
- export storage
- deployment operator / admin

In a recommended production deployment:

- the reverse proxy is the external boundary
- Waggle runs on a private network behind it
- Neo4j is not directly internet-accessible

## Authentication

Current Core mechanisms:

- API-key authentication for HTTP mode
- tenant isolation through issued keys and scoped operations

Current non-goals in Core:

- no built-in SSO
- no SCIM
- no enterprise identity provider integration

## Authorization

Current Core model:

- API keys authenticate callers
- tenants provide the primary isolation boundary

## Data storage

Waggle may store:

- transcript text
- extracted nodes and edges
- context windows
- export artifacts such as `.abhi`
- API-key and tenant metadata

Storage locations depend on deployment mode:

- local Core: SQLite
- remote/self-hosted production path: Neo4j

Encryption-at-rest note:

- SQLite is not encrypted at rest by Waggle itself
- infrastructure-level disk encryption remains the operator's responsibility
- `.abhi` exports support optional encryption

## Data deletion and retention

Current state:

- Core supports a per-tenant retention policy and manual prune runs
- operators configure retention windows with admin commands or environment-seeded defaults
- prune summaries are stored as administrative run records

Current limitation:

- Core does not yet run background pruning by itself; operators should schedule `prune-retention`
- future work can expand the current audit stream with richer export, auth-failure, and admin-read coverage

## Audit logging

Current state:

- application logging and metrics exist
- Core now stores append-only audit events with admin query support

Current coverage:

- API key creation, revocation, and HTTP use
- retention policy updates and prune runs
- node and edge create/update/delete operations
- HTTP graph reads for snapshots, transcript inspection, diff views, query/debug views, and export downloads
- backup, `.abhi`, and context-bundle exports
- backup and `.abhi` imports

Current limitation:

- coverage now includes the main HTTP graph-read surfaces, but it is still not exhaustive across every tool and internal code path

Audit caveat:

- application-level audit logs will not replace infrastructure or database-native audit controls in regulated deployments

## Authorization

Current Core model:

- API keys can carry least-privilege scopes
- MCP and HTTP graph routes enforce `graph:read` and `graph:write`
- HTTP admin routes enforce `admin:read` and `admin:write` when an API key is presented

Current limitation:

- identity federation and user-role enforcement remain Plus-only

## Network security

Recommended deployment:

- all external traffic over HTTPS
- reverse proxy terminates TLS
- Waggle only exposed through the proxy
- Neo4j restricted to private network access

Operator responsibilities:

- TLS termination
- DNS
- firewall rules
- host hardening
- network segmentation

## Secrets

Sensitive material may include:

- API keys
- Neo4j credentials
- Google Drive credentials if enabled
- exported memory artifacts

Recommended handling:

- store secrets in a secret manager
- do not commit `.env` files
- rotate credentials regularly
- revoke unused keys

## Deployment responsibility

For self-hosted deployments, the operator is responsible for:

- TLS and certificate management
- proxy configuration
- firewalling and network restrictions
- infrastructure backups
- host and container security
- secret storage
- access review

Waggle provides the application layer, not the entire infrastructure security program.

## Transcript Redaction

Waggle implements a configurable redaction layer that scrubs sensitive information (e.g. API keys, passwords, bearer tokens, and secrets) from transcript content before it is stored or processed for memory extraction.

### Behavior and Hardening
- **Pre-Persistence Scrubbing**: Redaction occurs at the entry point of the persistence layer. Verbatim transcripts written to the database (and metadata used to enrich knowledge nodes) are modified before serialization.
- **Non-Recoverable**: Once redacted, the original sensitive values are not written to durable storage and are completely unrecoverable.
- **Built-in Defaults**: Default redaction rules match common formats, including:
  - OpenAI-style and other API keys (`sk-...`)
  - HTTP Authorization `Bearer` tokens
  - Password assignments (`password=...`, `password: ...`, `passwd`, `pwd`)
  - General secret/credential keys (`secret=...`, `private_key: ...`, etc.)

### Custom Rules Configuration
Operators can enable redaction and configure custom regex patterns using environment variables. 

> [!IMPORTANT]
> If a valid JSON array is provided in `WAGGLE_REDACTION_RULES_JSON` and contains at least one valid rule, it **completely replaces (recommences instead of adds to)** the default built-in rules. If the JSON is empty, invalidly structured, or none of its entries are valid, the configuration logic safely **falls back to the default rules** to preserve security-first behavior.

```bash
export WAGGLE_REDACTION_ENABLED=true

export WAGGLE_REDACTION_RULES_JSON='[
  {
    "name":"custom_secret",
    "pattern":"SECRET_[A-Z0-9]+",
    "replacement":"[REDACTED_SECRET]"
  }
]'
```

### Limitations of Regex-Based Detection
- **Pattern Match Failure**: Simple regex-based patterns may fail to catch unstructured or uniquely formatted sensitive tokens.
- **Accidental Redaction**: Generous patterns might inadvertently replace benign conversation text matching similar shapes.
- **No Retrospective Action**: Redaction only applies to new incoming transcript turns. Previously stored database records are not retrospectively redacted.

## Known limitations

Current limitations:

- SSO and RBAC are not in Waggle Core
- audit querying is available through `list-audit-events`, but export/download and SIEM integrations are not built yet
- encryption at rest depends on deployment infrastructure
- retention enforcement is currently operator-triggered rather than background-scheduled
- some workflows remain SQLite-first or SQLite-only in v1

## Recommended companion docs

- [production deployment guide](../deployment/production.md)
- [hardening checklist](./hardening-checklist.md)
- [top-level security notes](../../SECURITY.md)
