# ABHI Format v2

`.abhi` is Waggle's portable memory artifact. Version 2 standardizes it as a zip container so it is inspectable with normal tooling while still supporting encryption, signatures, and deterministic diff/merge workflows.

## Container

- File extension: `.abhi`
- Physical container: ZIP archive
- Text encoding inside the archive: UTF-8
- Required top-level member: `manifest.json`
- Readers must reject unknown major schema versions with a clear error. For example, a reader that supports `2.x` must fail on `3.0.0`.

## Required Members

- `manifest.json`
- `transcripts.jsonl`
- `nodes.jsonl`
- `edges.jsonl`

## Optional Members

- `context_windows.jsonl`
- `signatures/content.ed25519`
- `signatures/public_key.pem`

## `manifest.json`

`manifest.json` is the entrypoint for the archive. Required fields:

```json
{
  "schema_version": "2.0.0",
  "tenant": "local-default",
  "agent_id": "codex",
  "project": "MCP",
  "session_id": "optional-session",
  "embedding_model_id": "all-MiniLM-L6-v2",
  "embedding_dim": 384,
  "encryption": {
    "enabled": false,
    "algorithm": ""
  },
  "signatures": {
    "algorithm": "ed25519",
    "present": false
  },
  "scope": "project",
  "includes_embeddings": true,
  "export_context": {},
  "counts": {
    "transcripts": 200,
    "nodes": 94,
    "edges": 41,
    "context_windows": 3
  },
  "members": {
    "transcripts.jsonl": {
      "sha256": "…",
      "size": 12345,
      "encrypted": false
    }
  },
  "content_hash": "…"
}
```

Rules:

- `schema_version` is mandatory.
- `embedding_model_id` and `embedding_dim` describe the embeddings present in the artifact.
- `members` records the per-member hash and encryption state.
- `export_context` is excluded from `content_hash` and must not be used for portable integrity decisions.
- `content_hash` is the canonical hash over the manifest-without-signature-state, minus `export_context`, plus the JSONL payloads.

## `transcripts.jsonl`

One transcript message per line. Fields:

```json
{
  "id": "uuid",
  "agent_id": "codex",
  "project": "MCP",
  "session_id": "session-a",
  "observed_at": "2026-05-01T12:34:56Z",
  "turn_index": 17,
  "role": "assistant",
  "transcript_text": "Verbatim text",
  "embedding_b64": "base64-encoded float32 little-endian embedding bytes",
  "embedding_model_id": "all-MiniLM-L6-v2",
  "embedding_dim": 384,
  "content_hash": "normalized-content-hash",
  "turn_pair_id": "optional-turn-pair-id",
  "metadata": {}
}
```

Rules:

- One line per message.
- `transcript_text` is verbatim unless export redaction is enabled.
- `embedding_b64` is included when `--include-embeddings` is enabled.

## `nodes.jsonl`

One extracted memory node per line. Fields:

```json
{
  "id": "uuid",
  "label": "Decision: use PostgreSQL",
  "content": "Use PostgreSQL for production.",
  "node_type": "decision",
  "tags": ["database"],
  "source_prompt": "optional provenance text",
  "source_turn_pair_id": "turn-pair-123",
  "embedding_b64": "base64-encoded float32 little-endian embedding bytes",
  "embedding_model_id": "all-MiniLM-L6-v2",
  "embedding_dim": 384,
  "metadata": {},
  "evidence_records": [],
  "created_at": "2026-05-01T12:34:56Z",
  "updated_at": "2026-05-01T12:34:56Z"
}
```

## `edges.jsonl`

One relationship per line:

```json
{
  "id": "uuid",
  "source_id": "node-a",
  "target_id": "node-b",
  "relationship": "depends_on",
  "weight": 1.0,
  "metadata": {},
  "created_at": "2026-05-01T12:34:56Z"
}
```

## `context_windows.jsonl`

Optional session or context-window metadata:

```json
{
  "id": "window-id",
  "repo_id": "repo-id",
  "session_id": "session-a",
  "title": "Planning thread",
  "status": "closed",
  "node_count": 14,
  "created_at": "2026-05-01T12:34:56Z",
  "updated_at": "2026-05-01T12:34:56Z"
}
```

## Encryption

- `--encrypt` encrypts each JSONL member independently with AES-256-GCM.
- Passphrases never leave the local machine.
- The zip container and manifest remain inspectable; encrypted members are stored as JSON envelopes containing nonce, salt, KDF metadata, and ciphertext.

## Signatures

- `--sign` signs `content_hash` with a local Ed25519 key.
- Keys live under `~/.waggle/keys/`.
- `signatures/content.ed25519` stores the detached signature.
- `signatures/public_key.pem` stores the public key needed for verification.
- Re-exporting the same logical graph preserves `content_hash`, so signatures remain valid across deterministic re-exports.

## Reader Behavior

- Reject unknown major versions.
- Verify `content_hash` before import.
- If `--verify-signature` is requested, fail closed on missing or invalid signatures.
- If `embedding_model_id` mismatches the active store, prompt or offer re-embedding instead of silently mixing models.

## Export Safety

Waggle's server and CLI wrappers run a conservative transcript secret scan before
exporting `.abhi` artifacts. The scan is intentionally biased toward refusal so
obvious credential or token shapes do not leave the machine by accident.

- If the scan finds likely secrets, export is refused by default.
- `--force` exists for maintainers who have reviewed the exact scope and
  confirmed the match is a false positive, synthetic fixture data, or otherwise
  safe to export.
- Avoid `--force` on live customer data. Redact the transcript or narrow the
  export scope first when there is any doubt.

## Determinism

The stable, portable surface is:

- sorted JSON object keys
- JSONL members written in a deterministic row order
- zip members written in a fixed order with fixed timestamps (`2000-01-01T00:00:00`)
- canonical content hashing

Byte-identical guarantees apply to unsigned, unencrypted exports of the same logical graph:

- exporting the same graph twice must yield identical archive bytes
- export -> import -> export must yield identical archive bytes

Fields that are intentionally outside portable hashing:

- `manifest.export_context`

Known exceptions:

- encrypted exports are not byte-identical because AES-GCM salts and nonces are randomized by design
