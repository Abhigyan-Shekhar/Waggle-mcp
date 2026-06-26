# Environment variables

Waggle reads the following `WAGGLE_*` environment variables in `src/waggle/config.py`. Values are parsed when `AppConfig.from_env()` starts the server or CLI command.

Boolean values are enabled only when set to the lowercase string `true`. Integer and float values must parse as their listed type.

## Core runtime

| Variable | Default | Type | Applies when | Example |
|----------|---------|------|--------------|---------|
| `WAGGLE_BACKEND` | `sqlite` | string enum: `sqlite`, `neo4j` | Always. Selects the storage backend. | `neo4j` |
| `WAGGLE_TRANSPORT` | `stdio` | string enum: `stdio`, `http` | Always. HTTP transport requires `WAGGLE_BACKEND=neo4j`. | `http` |
| `WAGGLE_MODEL` | `all-MiniLM-L6-v2` | string | Always. Names the sentence-transformers embedding model. | `BAAI/bge-small-en-v1.5` |
| `WAGGLE_EMBEDDING_BACKEND` | `pytorch` | string enum: `pytorch`, `onnx` | Always. Controls which backend Sentence Transformers uses for embedding inference. | `onnx` |
| `WAGGLE_DEFAULT_TENANT_ID` | `local-default` | string | Always. Used when a request does not provide a tenant. Must not be empty. | `workspace-default` |
| `WAGGLE_LOG_LEVEL` | `INFO` | string | Always. Configures application logging verbosity. | `DEBUG` |
| `WAGGLE_STARTUP_MODE` | `normal` | string enum: `fast`, `normal`, `strict` | Always. Controls startup/warmup behavior. | `strict` |

## Embedding backend

`WAGGLE_EMBEDDING_BACKEND` controls which backend Sentence Transformers uses for embedding inference.

**Default:** `pytorch`

**Allowed values:**

* `pytorch`
* `onnx`

Example:

```bash
export WAGGLE_EMBEDDING_BACKEND=onnx
```

### ONNX Runtime requirements

Using `WAGGLE_EMBEDDING_BACKEND=onnx` requires additional ONNX Runtime dependencies:

```bash
pip install onnxruntime optimum
```

### Backend tradeoffs

* **pytorch** (default): Recommended for most deployments. Simpler setup and works out of the box.
* **onnx**: Can improve inference performance and reduce latency on some hardware configurations, but requires additional ONNX Runtime dependencies.

If ONNX dependencies are unavailable, model initialization may fail and Waggle will fall back to deterministic embeddings.

## SQLite storage

| Variable | Default | Type | Applies when | Example |
|----------|---------|------|--------------|---------|
| `WAGGLE_DB_PATH` | `~/.waggle/waggle.db` (or the Codex Waggle DB path discovered from `~/.codex/config.toml`) | path string | `WAGGLE_BACKEND=sqlite`. Expanded with `~` support. | `/var/lib/waggle/waggle.db` |

## Database Path Resolution

When using the SQLite backend, Waggle determines the database path in the following order:

1. `WAGGLE_DB_PATH` if it is explicitly set.
2. `mcp_servers.waggle.env.WAGGLE_DB_PATH` from `~/.codex/config.toml` (only when `WAGGLE_DB_PATH` is not set).
3. The default path: `~/.waggle/waggle.db`.

The `~` character is expanded to the current user's home directory.
To force Waggle to use a specific database location, set `WAGGLE_DB_PATH` explicitly.

## HTTP service

| Variable | Default | Type | Applies when | Example |
|----------|---------|------|--------------|---------|
| `WAGGLE_HTTP_HOST` | `0.0.0.0` | string | HTTP server binding. | `127.0.0.1` |
| `WAGGLE_HTTP_PORT` | `8080` (falls back to `PORT` before `8080`) | integer | HTTP server binding. | `8080` |
| `WAGGLE_RATE_LIMIT_RPM` | `120` | integer | HTTP request rate limiting. | `240` |
| `WAGGLE_WRITE_RATE_LIMIT_RPM` | `60` | integer | HTTP write-tool rate limiting. | `120` |
| `WAGGLE_MAX_CONCURRENT_REQUESTS` | `8` | integer | HTTP request concurrency limiting. | `16` |
| `WAGGLE_MAX_PAYLOAD_BYTES` | `1048576` | integer | HTTP request body size limit. | `2097152` |
| `WAGGLE_REQUEST_TIMEOUT_SECONDS` | `30` | integer | Per-request timeout handling. | `60` |

## Neo4j storage

| Variable | Default | Type | Applies when | Example |
|----------|---------|------|--------------|---------|
| `WAGGLE_NEO4J_URI` | empty string | string | Required when `WAGGLE_BACKEND=neo4j`. | `bolt://localhost:7687` |
| `WAGGLE_NEO4J_USERNAME` | empty string | string | Required when `WAGGLE_BACKEND=neo4j`. | `neo4j` |
| `WAGGLE_NEO4J_PASSWORD` | empty string | string | Required when `WAGGLE_BACKEND=neo4j`. | `change-me` |
| `WAGGLE_NEO4J_DATABASE` | empty string | string | Optional with `WAGGLE_BACKEND=neo4j`; uses the driver's default database when empty. | `neo4j` |

## Retrieval and ranking

| Variable | Default | Type | Applies when | Example |
|----------|---------|------|--------------|---------|
| `WAGGLE_RECENCY_HALF_LIFE_DAYS` | `30.0` | float | Hybrid retrieval recency scoring. Must be greater than `0`. | `14.0` |
| `WAGGLE_HYBRID_VECTOR_WEIGHT` | `1.0` | float | Hybrid retrieval vector score weighting. | `1.2` |
| `WAGGLE_HYBRID_BM25_WEIGHT` | `1.0` | float | Hybrid retrieval BM25 score weighting. | `0.8` |
| `WAGGLE_HYBRID_GRAPH_WEIGHT` | `1.0` | float | Hybrid retrieval graph score weighting. | `1.5` |
| `WAGGLE_HYBRID_RECENCY_WEIGHT` | `1.0` | float | Hybrid retrieval recency score weighting. | `0.5` |
| `WAGGLE_HYBRID_RERANK_ENABLED` | `false` | boolean string | Hybrid retrieval reranking. Set to `true` to enable. | `true` |
| `WAGGLE_HYBRID_RERANK_MODEL` | `claude-3-5-sonnet-latest` | string | Used when hybrid reranking is enabled. | `claude-3-5-haiku-latest` |
| `WAGGLE_HYBRID_RERANK_TOP_K_IN` | `20` | integer | Candidate count passed into reranking. Must be at least `1`. | `30` |
| `WAGGLE_HYBRID_RERANK_TOP_K_OUT` | `5` | integer | Candidate count returned after reranking. Must be at least `1`. | `8` |
| `WAGGLE_TIERED_RETRIEVAL` | `false` | boolean string | Enables tiered retrieval when set to `true`. | `true` |
| `WAGGLE_TIERED_TOP_K_WINDOWS` | `3` | integer | Tiered retrieval window count. Must be at least `1`. | `5` |
| `WAGGLE_DEDUP_THRESHOLD` | `0.88` | float | Write-time canonicalization dedup threshold. Must be at least `0.85`. | `0.90` |

## Exports and retention

| Variable | Default | Type | Applies when | Example |
|----------|---------|------|--------------|---------|
| `WAGGLE_EXPORT_DIR` | unset | path string | Export commands and generated artifacts when an export directory is needed. | `/var/lib/waggle/exports` |
| `WAGGLE_RETENTION_ENABLED` | `false` | boolean string | Retention pruning. Set to `true` to enable. | `true` |
| `WAGGLE_RETENTION_DAYS` | `90` | integer | Retention pruning age. Must be at least `1`. | `180` |
| `WAGGLE_RETENTION_PRUNE_INTERVAL_HOURS` | `24` | integer | Background retention prune interval. Must be at least `1`. | `12` |
