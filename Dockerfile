# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────────────────────
# Waggle MCP — pre-baked image
# Model is downloaded at build time and stored in a named volume so every
# subsequent restart skips the download entirely (~3 s cold start vs ~30 s).
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Mount BuildKit cache dirs to avoid re-downloading on every build
ENV PIP_NO_CACHE_DIR=1

WORKDIR /build

# 1) Copy only dependency metadata first so Docker layer caching is preserved
COPY pyproject.toml README.md LICENSE ./

# 2) Install dependencies before copying source code
RUN pip install --upgrade pip && \
    pip install torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install ".[neo4j]"

# 3) Copy source code (changes here won't bust the dep layer)
COPY src ./src

# 4) Pre-download the embedding model so it is baked into the image layer
RUN HF_HOME=/root/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/root/.cache/sentence-transformers \
    python -c "from sentence_transformers import SentenceTransformer; \
               SentenceTransformer('all-MiniLM-L6-v2')"

# ──────────────────────────────────────────────────────────────────────────────
# Runtime image (re-uses the same base so layers are shared in the registry)
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# VERSION is injected at build time; defaults to the current release.
# To override: docker build --build-arg VERSION=0.1.14 .
ARG VERSION=0.1.14

LABEL org.opencontainers.image.title="waggle-mcp" \
      org.opencontainers.image.description="MCP server that gives LLMs persistent graph-structured memory" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.authors="Abhigyan Shekhar" \
      org.opencontainers.image.url="https://github.com/Abhigyan-Shekhar/Waggle-mcp" \
      org.opencontainers.image.source="https://github.com/Abhigyan-Shekhar/Waggle-mcp" \
      org.opencontainers.image.licenses="Apache-2.0"

# ── Cache directories (overridable via env / volume mounts) ──────────────────
# The model is baked in; /cache is a volume for runtime persistence of
# fine-tuned models or alternate checkpoints.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Hugging Face / sentence-transformers model cache
    HF_HOME=/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers \
    # Set OFFLINE only after the first run (cache is populated).
    # The entrypoint script can flip this automatically.
    TRANSFORMERS_OFFLINE=0 \
    # Waggle runtime defaults
    WAGGLE_TRANSPORT=stdio \
    WAGGLE_BACKEND=sqlite \
    WAGGLE_DB_PATH=/data/memory.db \
    WAGGLE_HTTP_HOST=0.0.0.0 \
    WAGGLE_HTTP_PORT=8080 \
    WAGGLE_DEFAULT_TENANT_ID=local-default \
    WAGGLE_MODEL=all-MiniLM-L6-v2 \
    WAGGLE_STARTUP_MODE=normal \
    WAGGLE_EXTRACT_BACKEND=auto \
    WAGGLE_LOG_LEVEL=INFO

# Copy the installed packages AND the pre-downloaded model cache from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /root/.cache /app/.cache

# Copy project source
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps -e .

# Non-root user (required by Glama security policy)
RUN useradd --no-create-home --shell /bin/false waggle && \
    mkdir -p /data /app/.cache && \
    chown -R waggle:waggle /app /data

USER waggle

# /data  → database (SQLite)
# /app/.cache → model files (populated at build; mountable for override)
VOLUME ["/data", "/app/.cache"]

# Only exposed when WAGGLE_TRANSPORT=http
EXPOSE 8080

ENTRYPOINT ["python", "-m", "waggle.server"]
CMD ["serve"]
