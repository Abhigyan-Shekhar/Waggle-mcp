from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waggle-server",
        description="Bundled Waggle MCP server runtime for Codex plugins.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve"],
        help="Only the MCP server command is available in the bundled runtime.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="Only stdio transport is available in the bundled runtime.",
    )
    parser.add_argument(
        "--server-info",
        action="store_true",
        help="Print bundled runtime compatibility metadata as JSON and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.server_info:
        from waggle.runtime_info import server_info_json

        print(server_info_json())
        return 0

    from waggle.config import AppConfig
    from waggle.server import run_stdio

    os.environ.setdefault("WAGGLE_TRANSPORT", "stdio")
    config = AppConfig.from_env()
    config.transport = "stdio"
    asyncio.run(run_stdio(config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
