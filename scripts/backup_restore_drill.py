#!/usr/bin/env python3
"""
backup_restore_drill.py
=======================
Python companion to backup_restore_drill.sh.
Provides structured output and --json flag for CI integration.

Usage:
    python scripts/backup_restore_drill.py \\
        --host http://localhost:8080 \\
        --api-key YOUR_KEY \\
        [--json]

Exit codes: 0 = all checks passed, 1 = one or more failures.
Requires: pip install httpx
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import httpx
except ImportError as exc:
    raise SystemExit("httpx is required: pip install httpx") from exc


@dataclass
class CheckResult:
    label: str
    passed: bool
    detail: str = ""


@dataclass
class DrillReport:
    host: str
    checks: list[CheckResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def add(self, label: str, passed: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(label=label, passed=passed, detail=detail))

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "duration_seconds": round(time.time() - self.started_at, 2),
            "passed": self.passed,
            "failed": self.failed,
            "status": "PASS" if self.failed == 0 else "FAIL",
            "checks": [
                {"label": c.label, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }

    def print_text(self) -> None:
        print("=" * 56)
        print("  waggle-mcp Backup/Restore Drill")
        print(f"  Host: {self.host}")
        print("=" * 56)
        for c in self.checks:
            icon = "✓" if c.passed else "✗"
            suffix = f" — {c.detail}" if c.detail else ""
            print(f"  [{icon}] {c.label}{suffix}")
        print("=" * 56)
        print(f"  RESULTS: {self.passed} passed, {self.failed} failed")
        print(f"  STATUS : {'PASS ✓' if self.failed == 0 else 'FAIL ✗'}")
        print("=" * 56)


def mcp_call(client: httpx.Client, host: str, api_key: str, tool: str, arguments: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    resp = client.post(
        f"{host}/mcp",
        json=payload,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def run(args: argparse.Namespace) -> DrillReport:
    report = DrillReport(host=args.host)

    with httpx.Client() as client:
        # ── 1. Health check ─────────────────────────────────────────────
        try:
            r = client.get(f"{args.host}/health/ready", timeout=5)
            report.add("health/ready returns 200", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:
            report.add("health/ready returns 200", False, str(exc))
            return report  # Can't continue if server is unreachable

        # ── 2. Seed three nodes ─────────────────────────────────────────
        node_ids: list[str] = []
        for i in range(1, 4):
            label = f"drill-node-{i}-{int(time.time())}"
            try:
                rsp = mcp_call(client, args.host, args.api_key, "store_node", {
                    "label": label,
                    "content": f"Backup/restore drill test node {i}.",
                    "node_type": "fact",
                })
                node_id = rsp["structuredContent"]["id"]
                node_ids.append(node_id)
                report.add(f"store node {i}", True, f"id={node_id}")
            except Exception as exc:
                report.add(f"store node {i}", False, str(exc))

        # ── 3. Stats before backup ──────────────────────────────────────
        stats_before = {}
        try:
            rsp = mcp_call(client, args.host, args.api_key, "get_stats", {})
            stats_before = rsp["structuredContent"]
            nodes_before = stats_before.get("total_nodes", 0)
            report.add("stats before backup", nodes_before >= 3, f"{nodes_before} nodes")
        except Exception as exc:
            report.add("stats before backup", False, str(exc))
            nodes_before = 0

        # ── 4. Export backup ────────────────────────────────────────────
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            backup_path = tf.name

        try:
            rsp = mcp_call(client, args.host, args.api_key, "export_graph_backup",
                           {"output_path": backup_path})
            sc = rsp["structuredContent"]
            exported_nodes = sc.get("node_count", -1)
            report.add("export backup", exported_nodes >= 3, f"{exported_nodes} nodes exported")
        except Exception as exc:
            report.add("export backup", False, str(exc))
            exported_nodes = -1

        # ── 5. Validate backup JSON ─────────────────────────────────────
        try:
            data = json.loads(Path(backup_path).read_text())
            valid = all(k in data for k in ("schema_version", "nodes", "edges", "tenant_id"))
            report.add("backup JSON structure", valid,
                       f"schema_version={data.get('schema_version','?')}, "
                       f"{len(data.get('nodes',[]))} nodes, {len(data.get('edges',[]))} edges")
        except Exception as exc:
            report.add("backup JSON structure", False, str(exc))

        # ── 6. Import backup ────────────────────────────────────────────
        try:
            rsp = mcp_call(client, args.host, args.api_key, "import_graph_backup",
                           {"input_path": backup_path})
            sc = rsp["structuredContent"]
            created = sc.get("nodes_created", 0)
            updated = sc.get("nodes_updated", 0)
            total = created + updated
            report.add("import backup", total >= 3,
                       f"nodes_created={created}, nodes_updated={updated}")
        except Exception as exc:
            report.add("import backup", False, str(exc))

        # ── 7. Post-import stats ─────────────────────────────────────────
        try:
            rsp = mcp_call(client, args.host, args.api_key, "get_stats", {})
            nodes_after = rsp["structuredContent"].get("total_nodes", 0)
            report.add("post-import node count ≥ pre-backup",
                       nodes_after >= nodes_before,
                       f"before={nodes_before}, after={nodes_after}")
        except Exception as exc:
            report.add("post-import stats", False, str(exc))

        # Cleanup temp file
        Path(backup_path).unlink(missing_ok=True)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="waggle-mcp backup/restore drill")
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--api-key", required=True, dest="api_key")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Output results as JSON (for CI)")
    args = parser.parse_args()

    report = run(args)

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        report.print_text()

    sys.exit(0 if report.failed == 0 else 1)


if __name__ == "__main__":
    main()
