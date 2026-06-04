from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)

# Tools that must NEVER trigger torch/sentence-transformers import.
# Queries to these tools are answered before the model is ready.
EMBEDDING_FREE_TOOLS: frozenset[str] = frozenset(
    {
        "list_tools",
        "get_stats",
        "list_context_scopes",
        "health",
        "graph_diff",
        "list_conflicts",
        "get_node_history",
        "timeline",
        "edge_quality_report",
    }
)

# Possible values surfaced in get_stats / health checks.
STATUS_NOT_STARTED = "not_started"
STATUS_WARMING_UP = "warming_up"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"  # fast / inspection mode


class EmbeddingModel:
    """Lazy-loaded sentence-transformer wrapper with optional background warmup.

    Lifecycle states
    ----------------
    not_started  - warmup has not been requested yet (default after construction)
    warming_up   - background thread is loading the model
    ready        - model loaded successfully; semantic calls are cheap
    failed       - background thread raised an exception; deterministic fallback active
    disabled     - startup_mode == "fast"; ML never loads
    """

    _DETERMINISTIC_MODELS = {"fake", "fake-model", "deterministic", "offline-demo"}

    _GLOBAL_EMBED_CACHE: OrderedDict[tuple[str, str], bytes] = OrderedDict()
    _GLOBAL_EMBED_CACHE_MAXSIZE = 512
    _GLOBAL_EMBED_CACHE_LOCK = threading.Lock()

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._fallback_to_deterministic = False

        # --- background-warmup state ---
        self._lock = threading.Lock()
        self._ready_event = threading.Event()
        self._warmup_started = False
        self._warmup_status: str = STATUS_NOT_STARTED
        self._warmup_error: str = ""

        # --- embedding LRU cache ---
        # Keyed by normalised text; value is a pre-computed bytes blob so we
        # can return a fresh np.frombuffer view on each hit without re-encoding.
        # 512 entries x 384 dims x 4 bytes ~= 750 KB -- negligible overhead.

    # ------------------------------------------------------------------
    # Public API: background warmup
    # ------------------------------------------------------------------

    def start_background_warmup(self) -> None:
        """Fire-and-forget model load.  Safe to call multiple times; only runs once."""
        with self._lock:
            if self._warmup_started:
                return
            # Deterministic models are always "ready" instantly — no thread needed.
            if self.uses_deterministic_mode:
                self._warmup_started = True
                self._warmup_status = STATUS_READY
                self._ready_event.set()
                return
            self._warmup_started = True
            self._warmup_status = STATUS_WARMING_UP

        thread = threading.Thread(target=self._background_load, daemon=True, name="waggle-embedding-warmup")
        thread.start()
        LOGGER.info("embedding_warmup_started", extra={"model": self.model_name})

    def disable_warmup(self) -> None:
        """Called in fast/inspection mode - mark as disabled so callers can surface it."""
        with self._lock:
            if self._warmup_started:
                return
            self._warmup_started = True
            self._warmup_status = STATUS_DISABLED
        self._ready_event.set()  # unblock any waiter immediately

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    @property
    def warmup_status(self) -> str:
        """One of: not_started | warming_up | ready | failed | disabled."""
        return self._warmup_status

    @property
    def warmup_error(self) -> str:
        """Non-empty string when warmup_status == 'failed'."""
        return self._warmup_error

    @property
    def is_ready(self) -> bool:
        return self._warmup_status == STATUS_READY

    # ------------------------------------------------------------------
    # Existing public API (unchanged contract)
    # ------------------------------------------------------------------

    @property
    def uses_deterministic_mode(self) -> bool:
        return self._fallback_to_deterministic or self.model_name.strip().lower() in self._DETERMINISTIC_MODELS

    @property
    def model_version(self) -> str:
        if self.uses_deterministic_mode:
            return "deterministic-v1"
        # Eagerly resolve the model so the version string is stable and matches
        # what will actually be used for embeddings (avoids cache key mismatch).
        with self._lock:
            model = self._model
        if model is None and not self._warmup_started:
            # Trigger synchronous on-demand load to resolve the real version.
            model = self._resolve_model(wait_timeout=60.0)
        if model is None:
            return "deterministic-v1"
        version = getattr(model, "__version__", None)
        if version:
            return str(version)
        return f"{model.__class__.__module__}.{model.__class__.__name__}"

    @property
    def model_id(self) -> str:
        name = self.model_name.strip() or "unknown"
        return f"{name}:{self.model_version}"

    @property
    def model(self) -> Any:
        """Return the loaded model (or None if not yet ready / deterministic mode)."""
        if self.uses_deterministic_mode:
            return None
        with self._lock:
            return self._model

    def embed(self, text: str, *, wait_timeout: float = 30.0) -> np.ndarray:
        """Embed *text*.

        If the background warmup is still in progress, block up to *wait_timeout*
        seconds.  After that, or if warmup failed, fall back to deterministic
        embeddings instead of raising an exception.

        Results are cached (LRU, 512 entries) so repeated queries within a
        session avoid a second model forward pass.
        """
        normalized = text.strip()
        if not normalized:
            raise ValueError("Cannot embed empty text.")

        cache_key = (self.model_name, normalized)

        # Fast path: return cached embedding (copy so callers can mutate freely)
        with EmbeddingModel._GLOBAL_EMBED_CACHE_LOCK:
            if cache_key in EmbeddingModel._GLOBAL_EMBED_CACHE:
                EmbeddingModel._GLOBAL_EMBED_CACHE.move_to_end(cache_key)
                return np.frombuffer(EmbeddingModel._GLOBAL_EMBED_CACHE[cache_key], dtype=np.float32).copy()

        if self.uses_deterministic_mode:
            # Canonical deterministic path — always safe to cache.
            result = self._embed_deterministically(normalized)
            should_cache = True
        else:
            model = self._resolve_model(wait_timeout)
            if model is None:
                # Transient fallback: warmup timed-out or failed.
                # Do NOT cache — once the model finishes loading, subsequent
                # calls should get real embeddings, not stale deterministic ones.
                result = self._embed_deterministically(normalized)
                should_cache = False
            else:
                result = np.asarray(
                    model.encode(
                        normalized,
                        normalize_embeddings=True,
                        convert_to_numpy=True,
                    ),
                    dtype=np.float32,
                )
                should_cache = True

        # Store in cache only when the result is canonical (not a transient fallback)
        if should_cache:
            blob = result.tobytes()
            with EmbeddingModel._GLOBAL_EMBED_CACHE_LOCK:
                if cache_key in EmbeddingModel._GLOBAL_EMBED_CACHE:
                    EmbeddingModel._GLOBAL_EMBED_CACHE.move_to_end(cache_key)
                else:
                    if len(EmbeddingModel._GLOBAL_EMBED_CACHE) >= EmbeddingModel._GLOBAL_EMBED_CACHE_MAXSIZE:
                        EmbeddingModel._GLOBAL_EMBED_CACHE.popitem(last=False)

                EmbeddingModel._GLOBAL_EMBED_CACHE[cache_key] = blob
        return result

    def embed_batch(self, texts: list[str], *, wait_timeout: float = 30.0) -> np.ndarray:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        normalized = [text.strip() for text in texts]
        if any(not text for text in normalized):
            raise ValueError("Cannot embed empty text values.")
        if self.uses_deterministic_mode:
            return np.asarray([self._embed_deterministically(t) for t in normalized], dtype=np.float32)

        model = self._resolve_model(wait_timeout)
        if model is None:
            return np.asarray([self._embed_deterministically(t) for t in normalized], dtype=np.float32)
        batch_size = min(64, max(1, len(normalized)))
        return np.asarray(
            model.encode(
                normalized,
                batch_size=batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ),
            dtype=np.float32,
        )

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        a_vec = np.asarray(a, dtype=np.float32)
        b_vec = np.asarray(b, dtype=np.float32)
        if a_vec.size == 0 or b_vec.size == 0:
            return 0.0
        if a_vec.shape != b_vec.shape:
            return 0.0
        a_norm = float(np.linalg.norm(a_vec))
        b_norm = float(np.linalg.norm(b_vec))
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a_vec, b_vec) / (a_norm * b_norm))

    @staticmethod
    def to_bytes(embedding: np.ndarray) -> bytes:
        return np.asarray(embedding, dtype=np.float32).tobytes()

    @staticmethod
    def from_bytes(data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_model(self, wait_timeout: float) -> Any:
        """Return the loaded model, waiting if warmup is in progress.

        Returns None if warmup failed or was disabled (caller should fall back
        to deterministic embeddings).
        """
        if not self._warmup_started:
            # Synchronous on-demand load (legacy / direct embed() without warmup).
            try:
                loaded = self._load_transformer_model()
                with self._lock:
                    self._model = loaded
                    self._warmup_started = True
                    if loaded is not None:
                        self._warmup_status = STATUS_READY
                    else:
                        self._warmup_status = STATUS_FAILED
                self._ready_event.set()
                return loaded
            except Exception as exc:
                self._handle_warmup_failure(exc)
                return None

        # Warmup was requested - wait for it.
        if not self._ready_event.wait(timeout=wait_timeout):
            LOGGER.warning(
                "embedding_warmup_timeout",
                extra={"model": self.model_name, "timeout": wait_timeout},
            )
            return None

        with self._lock:
            if self._warmup_status in (STATUS_FAILED, STATUS_DISABLED):
                return None
            return self._model

    def _background_load(self) -> None:
        try:
            loaded = self._load_transformer_model()
            with self._lock:
                self._model = loaded
                if loaded is not None:
                    self._warmup_status = STATUS_READY
                    LOGGER.info("embedding_warmup_complete", extra={"model": self.model_name})
                else:
                    self._warmup_status = STATUS_FAILED
                    self._warmup_error = "model returned None after load"
                    LOGGER.warning("embedding_warmup_produced_none", extra={"model": self.model_name})
        except Exception as exc:
            self._handle_warmup_failure(exc)
        finally:
            self._ready_event.set()

    def _handle_warmup_failure(self, exc: Exception) -> None:
        self._fallback_to_deterministic = True
        with self._lock:
            self._warmup_status = STATUS_FAILED
            self._warmup_error = str(exc)
        LOGGER.warning(
            "embedding_warmup_failed",
            extra={"model": self.model_name, "error": str(exc)},
        )

    def _load_transformer_model(self) -> Any:
        from sentence_transformers import SentenceTransformer

        try:
            return SentenceTransformer(self.model_name, local_files_only=True)
        except Exception:
            # Model not cached locally - must download from HuggingFace.
            # This can take 30-120 s and requires a network connection.
            # Tip: set WAGGLE_MODEL=deterministic for offline-safe mode.
            LOGGER.warning(
                "embedding_model_downloading",
                extra={
                    "model": self.model_name,
                    "tip": (
                        "Model not in local cache. Downloading ~420 MB from HuggingFace. "
                        "This will block the current call. "
                        "To avoid: set WAGGLE_MODEL=deterministic for offline-safe mode, "
                        'or pre-download: python -c "from sentence_transformers import SentenceTransformer; '
                        f"SentenceTransformer('{self.model_name}')\""
                    ),
                },
            )
            return SentenceTransformer(self.model_name)

    def _embed_deterministically(self, text: str) -> np.ndarray:
        vector = np.zeros(256, dtype=np.float32)
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for offset in range(0, len(digest), 4):
                bucket = digest[offset] % len(vector)
                weight = 1.0 + (digest[offset + 1] / 255.0)
                vector[bucket] += weight
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm
