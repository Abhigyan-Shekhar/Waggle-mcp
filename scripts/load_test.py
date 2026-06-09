#!/usr/bin/env python3
"""
waggle-mcp load tester
============================
Sends concurrent HTTP MCP requests to the running service and reports
p50 / p95 / p99 latency, throughput, and error rates.

Usage
-----
python scripts/load_test.py \\
    --host http://localhost:8080 \\
    --api-key  YOUR_KEY \\
    --tenant   workspace-default \\
    --duration 60 \\
    --concurrency 20 \\
    --write-ratio 0.3

Requirements: pip install httpx
"""
from __future__ import annotations

import argparse
import asyncio
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import httpx
except ImportError as exc:
    raise SystemExit("httpx is required: pip install httpx") from exc


# ---------------------------------------------------------------------------
# Sample payloads
# ---------------------------------------------------------------------------

ADJECTIVES = ["fast", "slow", "large", "tiny", "bright", "dark", "smart", "simple"]
NOUNS = ["node", "edge", "graph", "memory", "fact", "entity", "concept", "preference"]
NODE_TYPES = ["fact", "entity", "concept", "preference", "decision", "question", "note"]


def _rand_label() -> str:
    return f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{''.join(random.choices(string.ascii_lowercase, k=4))}"


def _rand_content() -> str:
    words = [random.choice(ADJECTIVES + NOUNS) for _ in range(random.randint(8, 20))]
    return " ".join(words).capitalize() + "."


def _store_node_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": random.randint(1, 999999),
        "method": "tools/call",
        "params": {
            "name": "store_node",
            "arguments": {
                "label": _rand_label(),
                "content": _rand_content(),
                "node_type": random.choice(NODE_TYPES),
                "tags": [random.choice(NOUNS)],
            },
        },
    }


def _query_graph_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": random.randint(1, 999999),
        "method": "tools/call",
        "params": {
            "name": "query_graph",
            "arguments": {
                "query": f"{random.choice(ADJECTIVES)} {random.choice(NOUNS)}",
                "max_nodes": 10,
            },
        },
    }


def _get_stats_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": random.randint(1, 999999),
        "method": "tools/call",
        "params": {"name": "get_stats", "arguments": {}},
    }


def _observe_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": random.randint(1, 999999),
        "method": "tools/call",
        "params": {
            "name": "observe_conversation",
            "arguments": {
                "user_message": _rand_content(),
                "assistant_response": _rand_content(),
            },
        },
    }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class Result:
    latency: float
    status: int
    is_write: bool
    error: str = ""


@dataclass
class Stats:
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)

    def report(self, duration: float) -> None:
        total = len(self.results)
        errors = [r for r in self.results if r.error or r.status >= 400]
        writes = [r for r in self.results if r.is_write]
        reads = [r for r in self.results if not r.is_write]
        latencies = sorted(r.latency for r in self.results)

        def pct(lst: list[float], p: float) -> float:
            if not lst:
                return 0.0
            idx = max(0, int(len(lst) * p / 100) - 1)
            return lst[idx]

        all_lat = sorted(r.latency for r in self.results)
        write_lat = sorted(r.latency for r in writes)
        read_lat = sorted(r.latency for r in reads)

        print("\n" + "=" * 60)
        print("LOAD TEST RESULTS")
        print("=" * 60)
        print(f"Duration   : {duration:.1f}s")
        print(f"Requests   : {total}  ({len(writes)} writes, {len(reads)} reads)")
        print(f"RPS        : {total / duration:.1f}")
        print(f"Errors     : {len(errors)}  ({100 * len(errors) / max(total, 1):.1f}%)")
        print()
        print("Latency (all):")
        print(f"  p50 = {pct(all_lat, 50)*1000:.1f}ms")
        print(f"  p95 = {pct(all_lat, 95)*1000:.1f}ms")
        print(f"  p99 = {pct(all_lat, 99)*1000:.1f}ms")
        print(f"  max = {max(latencies, default=0)*1000:.1f}ms")
        if write_lat:
            print("Latency (writes):")
            print(f"  p50 = {pct(write_lat, 50)*1000:.1f}ms")
            print(f"  p95 = {pct(write_lat, 95)*1000:.1f}ms")
        if read_lat:
            print("Latency (reads):")
            print(f"  p50 = {pct(read_lat, 50)*1000:.1f}ms")
            print(f"  p95 = {pct(read_lat, 95)*1000:.1f}ms")
        if errors:
            print()
            print("Error breakdown:")
            breakdown: dict[str, int] = {}
            for r in errors:
                key = r.error or f"HTTP {r.status}"
                breakdown[key] = breakdown.get(key, 0) + 1
            for msg, count in sorted(breakdown.items(), key=lambda x: -x[1])[:10]:
                print(f"  {count:5d}x  {msg}")
        print("=" * 60)


async def worker(
    client: httpx.AsyncClient,
    host: str,
    api_key: str,
    stats: Stats,
    write_ratio: float,
    stop_event: asyncio.Event,
    lock: asyncio.Lock,
) -> None:
    while not stop_event.is_set():
        is_write = random.random() < write_ratio
        if is_write:
            payload = random.choice([_store_node_payload, _observe_payload])()
        else:
            payload = random.choice([_query_graph_payload, _get_stats_payload])()

        started = time.perf_counter()
        error = ""
        status = 0
        try:
            resp = await client.post(
                f"{host}/mcp",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
                timeout=35.0,
            )
            status = resp.status_code
            if status >= 400:
                error = resp.text[:120]
        except httpx.TimeoutException:
            error = "timeout"
        except httpx.RequestError as exc:
            error = f"request_error: {exc}"
        finally:
            elapsed = time.perf_counter() - started
            async with lock:
                stats.add(Result(latency=elapsed, status=status, is_write=is_write, error=error))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    stats = Stats()
    stop_event = asyncio.Event()
    lock = asyncio.Lock()

    print(f"Starting load test → {args.host}/mcp")
    print(f"  Concurrency : {args.concurrency}")
    print(f"  Duration    : {args.duration}s")
    print(f"  Write ratio : {args.write_ratio:.0%}")

    async with httpx.AsyncClient(http2=True) as client:
        # Warm-up single request
        try:
            probe = await client.get(f"{args.host}/health/ready", timeout=5)
            print(f"  Health check: HTTP {probe.status_code}")
        except Exception as exc:
            raise SystemExit(f"Cannot reach {args.host}: {exc}") from exc

        start = time.perf_counter()
        workers = [
            asyncio.create_task(worker(client, args.host, args.api_key, stats, args.write_ratio, stop_event, lock))
            for _ in range(args.concurrency)
        ]
        await asyncio.sleep(args.duration)
        stop_event.set()
        await asyncio.gather(*workers, return_exceptions=True)
        elapsed = time.perf_counter() - start

    stats.report(elapsed)


def main() -> None:
    parser = argparse.ArgumentParser(description="waggle-mcp load tester")
    parser.add_argument("--host", default="http://localhost:8080", help="Base URL of the MCP server")
    parser.add_argument("--api-key", required=True, dest="api_key", help="X-API-Key value")
    parser.add_argument("--duration", type=float, default=60.0, help="Test duration in seconds")
    parser.add_argument("--concurrency", type=int, default=10, help="Number of concurrent workers")
    parser.add_argument("--write-ratio", type=float, default=0.3, dest="write_ratio", help="Fraction of requests that are writes [0-1]")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
