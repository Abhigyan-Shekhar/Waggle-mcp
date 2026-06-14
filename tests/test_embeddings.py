from __future__ import annotations

import builtins
import threading
from typing import Any

import numpy as np
import pytest

from waggle.embeddings import EmbeddingModel, get_embedding_model


def test_embedding_bytes_round_trip() -> None:
    vector = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    encoded = EmbeddingModel.to_bytes(vector)
    decoded = EmbeddingModel.from_bytes(encoded)
    assert np.allclose(decoded, vector)


def test_cosine_similarity_handles_orthogonal_vectors() -> None:
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert EmbeddingModel.cosine_similarity(a, b) == 0.0


def test_cosine_similarity_returns_zero_for_shape_mismatch() -> None:
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    b = np.array([1.0, 0.0], dtype=np.float32)
    assert EmbeddingModel.cosine_similarity(a, b) == 0.0


def test_fake_model_is_deterministic_and_normalized() -> None:
    model = EmbeddingModel("fake-model")
    other_model = EmbeddingModel("fake-model")
    a = model.embed("PostgreSQL over MySQL")
    b = other_model.embed("PostgreSQL over MySQL")
    c = model.embed("Dark mode UI")

    assert np.allclose(a, b)
    assert np.isclose(np.linalg.norm(a), 1.0)
    assert EmbeddingModel.cosine_similarity(a, c) < 1.0


def test_uncached_transformer_falls_back_to_deterministic_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    def uncached(_: EmbeddingModel) -> None:
        raise OSError("model cache missing")

    monkeypatch.setattr(EmbeddingModel, "_load_transformer_model", uncached)

    model = EmbeddingModel("all-MiniLM-L6-v2")
    vector = model.embed("Backend uses FastAPI")

    assert model.uses_deterministic_mode is True
    assert vector.shape == (256,)
    assert np.isclose(np.linalg.norm(vector), 1.0)


def test_embedding_cache_shared_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    EmbeddingModel._GLOBAL_EMBED_CACHE.clear()

    class CountingModel:
        def __init__(self) -> None:
            self.calls = 0

        def encode(
            self,
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
        ):
            self.calls += 1
            return np.array([1.0, 2.0, 3.0], dtype=np.float32)

    counting_model = CountingModel()

    monkeypatch.setattr(
        EmbeddingModel,
        "_resolve_model",
        lambda self, timeout: counting_model,
    )

    model_a = EmbeddingModel("shared-model")
    model_b = EmbeddingModel("shared-model")

    model_a.embed("foo")
    model_b.embed("foo")

    assert counting_model.calls == 1


def test_embedding_cache_isolates_different_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    EmbeddingModel._GLOBAL_EMBED_CACHE.clear()

    calls = {"a": 0, "b": 0}

    def resolve_model(self, timeout):
        if self.model_name == "model-a":

            class ModelA:
                def encode(
                    self,
                    text,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                ):
                    calls["a"] += 1
                    return np.array([1.0, 2.0, 3.0], dtype=np.float32)

            return ModelA()

        class ModelB:
            def encode(
                self,
                text,
                normalize_embeddings=True,
                convert_to_numpy=True,
            ):
                calls["b"] += 1
                return np.array([4.0, 5.0], dtype=np.float32)

        return ModelB()

    monkeypatch.setattr(
        EmbeddingModel,
        "_resolve_model",
        resolve_model,
    )

    model_a = EmbeddingModel("model-a")
    model_b = EmbeddingModel("model-b")

    vec_a = model_a.embed("foo")
    vec_b = model_b.embed("foo")

    assert calls["a"] == 1
    assert calls["b"] == 1

    assert vec_a.shape == (3,)
    assert vec_b.shape == (2,)


def test_get_embedding_model_returns_shared_instance() -> None:
    model_a = get_embedding_model("shared-loader-model")
    model_b = get_embedding_model("shared-loader-model")

    assert model_a is model_b
    assert model_a.model_name == model_b.model_name


def test_embedding_model_loads_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    load_calls: list[str] = []

    def fake_load(self) -> Any:
        load_calls.append("loaded")

        class DummyModel:
            def encode(self, text, normalize_embeddings=True, convert_to_numpy=True):
                return np.array([1.0, 1.0, 1.0], dtype=np.float32)

        return DummyModel()

    monkeypatch.setattr(EmbeddingModel, "_load_transformer_model", fake_load)

    model = get_embedding_model("singleton-init-model")
    model.embed("first")
    model.embed("second")

    assert len(load_calls) == 1


def test_deterministic_mode_does_not_import_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise AssertionError("sentence-transformers must not be imported in deterministic mode")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    model = get_embedding_model("deterministic")
    vector = model.embed("offline-safe request")

    assert model.uses_deterministic_mode is True
    assert vector.shape == (256,)
    assert np.isclose(np.linalg.norm(vector), 1.0)


def test_get_embedding_model_thread_safe_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    load_count = 0
    started = threading.Event()
    proceed = threading.Event()

    def fake_load(self) -> Any:
        nonlocal load_count
        load_count += 1
        started.set()
        proceed.wait(timeout=5)

        class DummyModel:
            def encode(self, text, normalize_embeddings=True, convert_to_numpy=True):
                return np.array([1.0, 1.0, 1.0], dtype=np.float32)

        return DummyModel()

    monkeypatch.setattr(EmbeddingModel, "_load_transformer_model", fake_load)

    model = get_embedding_model("thread-safe-model")

    def worker() -> None:
        model.embed("race")

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()

    assert started.wait(timeout=5)
    proceed.set()

    for thread in threads:
        thread.join(timeout=5)

    assert load_count == 1
