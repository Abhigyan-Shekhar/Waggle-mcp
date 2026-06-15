"""
Benchmark script comparing SentenceTransformer embedding latency and memory usage.

This script measures the performance of the all-MiniLM-L6-v2 model using two backends:
- PyTorch (standard SentenceTransformer)
- ONNX Runtime (optimized inference)

Metrics collected:
- Median latency (ms)
- P95 latency (ms)
- Throughput (texts/sec)
- Peak memory usage (MB)

Results are printed as a Markdown table for easy comparison.

Usage:
    python scripts/benchmark_embed.py

The script is deterministic and reproducible thanks to seeded randomness and warmup runs.
"""

from __future__ import annotations

import gc
import platform
import random
import statistics
import time
import tracemalloc
from typing import Any

# Configuration constants ? edit these to customize the benchmark
MODEL_NAME = "all-MiniLM-L6-v2"
BATCH_SIZES = [1, 8, 32, 64]
SINGLE_ITERATIONS = 100
BATCH_ITERATIONS = 20
WARMUP_RUNS = 5
RANDOM_SEED = 42


def generate_test_sentences(num_sentences: int, seed: int = RANDOM_SEED) -> list[str]:
    """Generate deterministic test sentences for benchmark.

    Uses seeded randomness to produce the same sentences every run, ensuring
    reproducibility. Sentences are health-related to be contextually meaningful.

    Args:
        num_sentences: Number of sentences to generate.
        seed: Random seed for reproducibility.

    Returns:
        List of test sentences.
    """
    random.seed(seed)

    health_topics = [
        "patient",
        "doctor",
        "hospital",
        "treatment",
        "medication",
        "diagnosis",
        "symptoms",
        "recovery",
        "surgery",
        "therapy",
        "disease",
        "health",
        "immune",
        "blood",
        "heart",
        "infection",
        "vaccine",
        "clinical",
        "research",
        "medicine",
    ]

    verbs = [
        "improve",
        "treat",
        "prevent",
        "diagnose",
        "monitor",
        "develop",
        "reduce",
        "increase",
        "analyze",
        "assess",
    ]

    adjectives = [
        "effective",
        "safe",
        "rapid",
        "comprehensive",
        "personalized",
        "advanced",
        "modern",
        "innovative",
        "reliable",
        "critical",
    ]

    sentences = []
    for _ in range(num_sentences):
        topic = random.choice(health_topics)
        verb = random.choice(verbs)
        adj = random.choice(adjectives)
        sentence = f"The {adj} {topic} {verb} outcomes through evidence-based research."
        sentences.append(sentence)

    return sentences


def benchmark_single(model: Any, sentence: str, backend_name: str) -> dict[str, float]:
    """Benchmark single-text embedding with latency and memory profiling.

    Runs WARMUP_RUNS iterations to warm up the model (discarded), then
    SINGLE_ITERATIONS timed iterations to collect statistics. Captures peak
    memory usage during the measurement window.

    Args:
        model: SentenceTransformer model instance.
        sentence: Single sentence to embed.
        backend_name: Name of backend for logging (e.g., "PyTorch", "ONNX").

    Returns:
        Dictionary with keys: median_ms, p95_ms, throughput_txs, memory_mb.
    """
    # Warm up: discard results
    for _ in range(WARMUP_RUNS):
        _ = model.encode(sentence, normalize_embeddings=True, convert_to_numpy=True)

    # Force garbage collection to establish a clean baseline
    gc.collect()

    # Measure: collect latencies
    latencies: list[float] = []
    tracemalloc.start()

    for _ in range(SINGLE_ITERATIONS):
        start = time.perf_counter()
        _ = model.encode(sentence, normalize_embeddings=True, convert_to_numpy=True)
        end = time.perf_counter()
        latencies.append((end - start) * 1000)  # Convert to milliseconds

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Calculate statistics
    median_latency = statistics.median(latencies)
    p95_latency = sorted(latencies)[int(0.95 * (len(latencies) - 1))]
    throughput = 1000.0 / median_latency  # texts per second
    memory_mb = peak / (1024 * 1024)

    return {
        "median_ms": median_latency,
        "p95_ms": p95_latency,
        "throughput_txs": throughput,
        "memory_mb": memory_mb,
    }


def benchmark_batch(model: Any, sentences: list[str], batch_size: int, backend_name: str) -> dict[str, float]:
    """Benchmark batch embedding with latency and memory profiling.

    Similar to benchmark_single but processes multiple sentences at once.
    Throughput is calculated as batch_size ? 1000 / median_latency.

    Args:
        model: SentenceTransformer model instance.
        sentences: List of sentences to embed.
        batch_size: Number of sentences per batch.
        backend_name: Name of backend for logging.

    Returns:
        Dictionary with keys: median_ms, p95_ms, throughput_txs, memory_mb.
    """
    # Use the first batch_size sentences
    batch = sentences[:batch_size]

    # Warm up: discard results
    for _ in range(WARMUP_RUNS):
        _ = model.encode(batch, normalize_embeddings=True, convert_to_numpy=True)

    # Force garbage collection
    gc.collect()

    # Measure: collect latencies
    latencies: list[float] = []
    tracemalloc.start()

    for _ in range(BATCH_ITERATIONS):
        start = time.perf_counter()
        _ = model.encode(batch, normalize_embeddings=True, convert_to_numpy=True)
        end = time.perf_counter()
        latencies.append((end - start) * 1000)  # Convert to milliseconds

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Calculate statistics
    median_latency = statistics.median(latencies)
    p95_latency = sorted(latencies)[int(0.95 * (len(latencies) - 1))]
    throughput = (batch_size * 1000.0) / median_latency  # texts per second
    memory_mb = peak / (1024 * 1024)

    return {
        "median_ms": median_latency,
        "p95_ms": p95_latency,
        "throughput_txs": throughput,
        "memory_mb": memory_mb,
    }


def print_markdown_table(results: list[dict[str, Any]]) -> None:
    """Print results as a clean Markdown table.

    Args:
        results: List of result dictionaries with keys: backend, mode, batch_size,
                 median_ms, p95_ms, throughput_txs, memory_mb.
    """
    print("\n## Embedding Latency & Memory Benchmark Results\n")

    # Table header
    print("| Backend | Mode | Batch Size | Median (ms) | P95 (ms) | Throughput (txt/s) | Peak Memory (MB) |")
    print("|---------|------|------------|-------------|---------|--------------------|--------------------|")

    # Table rows
    for row in results:
        print(
            f"| {row['backend']:<8} | {row['mode']:<5} | {row['batch_size']:>10} | "
            f"{row['median_ms']:>11.2f} | {row['p95_ms']:>7.2f} | {row['throughput_txs']:>18.2f} | "
            f"{row['memory_mb']:>16.2f} |"
        )


def print_hardware_info() -> None:
    """Print hardware and environment information for result reproducibility."""
    print("\n## Hardware & Environment\n")
    print(f"- **Python:** {platform.python_version()}")
    print(f"- **Platform:** {platform.system()} {platform.release()}")
    print(f"- **Processor:** {platform.processor()}")

    try:
        import sentence_transformers

        print(f"- **sentence-transformers:** {sentence_transformers.__version__}")
    except Exception:
        pass

    try:
        import onnxruntime

        print(f"- **onnxruntime:** {onnxruntime.__version__}")
    except Exception:
        pass


def main() -> None:
    """Main benchmark orchestration.

    Loads both PyTorch and ONNX backends, generates test data once,
    runs benchmarks across all batch sizes, and prints a comprehensive
    Markdown table with hardware information.
    """
    from sentence_transformers import SentenceTransformer

    print(f"Benchmarking {MODEL_NAME} embedding performance...")
    print("Model will run on CPU only (no CUDA).")

    # Generate test sentences once, reuse across all benchmarks
    max_batch_size = max(BATCH_SIZES)
    test_sentences = generate_test_sentences(num_sentences=max_batch_size)
    single_sentence = test_sentences[0]

    results: list[dict[str, Any]] = []

    # ?????????????????????????????????????????????????????????????????
    # Benchmark PyTorch backend (standard SentenceTransformer)
    # ?????????????????????????????????????????????????????????????????
    print("\n[1/2] Loading PyTorch backend...")
    try:
        model_pytorch = SentenceTransformer(MODEL_NAME, device="cpu")
        print("      ? PyTorch backend loaded successfully")

        # Single-text benchmark
        print("      Running single-text benchmark...")
        metrics = benchmark_single(model_pytorch, single_sentence, "PyTorch")
        results.append(
            {
                "backend": "PyTorch",
                "mode": "single",
                "batch_size": 1,
                "median_ms": metrics["median_ms"],
                "p95_ms": metrics["p95_ms"],
                "throughput_txs": metrics["throughput_txs"],
                "memory_mb": metrics["memory_mb"],
            }
        )

        # Batch benchmarks
        print("      Running batch benchmarks...")
        for batch_size in BATCH_SIZES:
            metrics = benchmark_batch(model_pytorch, test_sentences, batch_size, "PyTorch")
            results.append(
                {
                    "backend": "PyTorch",
                    "mode": "batch",
                    "batch_size": batch_size,
                    "median_ms": metrics["median_ms"],
                    "p95_ms": metrics["p95_ms"],
                    "throughput_txs": metrics["throughput_txs"],
                    "memory_mb": metrics["memory_mb"],
                }
            )

        del model_pytorch
        gc.collect()

    except Exception as e:
        print(f"      ? PyTorch backend failed: {e}")
        print("      Skipping PyTorch benchmarks")

    # ?????????????????????????????????????????????????????????????????
    # Benchmark ONNX Runtime backend
    # ?????????????????????????????????????????????????????????????????
    print("\n[2/2] Loading ONNX Runtime backend...")
    try:
        model_onnx = SentenceTransformer(MODEL_NAME, backend="onnx", device="cpu")
        print("      ? ONNX Runtime backend loaded successfully")

        # Single-text benchmark
        print("      Running single-text benchmark...")
        metrics = benchmark_single(model_onnx, single_sentence, "ONNX")
        results.append(
            {
                "backend": "ONNX",
                "mode": "single",
                "batch_size": 1,
                "median_ms": metrics["median_ms"],
                "p95_ms": metrics["p95_ms"],
                "throughput_txs": metrics["throughput_txs"],
                "memory_mb": metrics["memory_mb"],
            }
        )

        # Batch benchmarks
        print("      Running batch benchmarks...")
        for batch_size in BATCH_SIZES:
            metrics = benchmark_batch(model_onnx, test_sentences, batch_size, "ONNX")
            results.append(
                {
                    "backend": "ONNX",
                    "mode": "batch",
                    "batch_size": batch_size,
                    "median_ms": metrics["median_ms"],
                    "p95_ms": metrics["p95_ms"],
                    "throughput_txs": metrics["throughput_txs"],
                    "memory_mb": metrics["memory_mb"],
                }
            )

        del model_onnx
        gc.collect()

    except Exception as e:
        print(f"      ? ONNX Runtime backend failed: {e}")
        print("      Skipping ONNX benchmarks")
        print("      Hint: install onnxruntime with: pip install onnxruntime optimum")

    # Print results
    print_markdown_table(results)
    print_hardware_info()

    print("\n? Benchmark complete!")


if __name__ == "__main__":
    main()
