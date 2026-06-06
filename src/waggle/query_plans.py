from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Sequence
from typing import Any

LOGGER = logging.getLogger(__name__)
WAGGLE_LOG_QUERY_PLANS = os.environ.get("WAGGLE_LOG_QUERY_PLANS", "false").strip().lower() == "true"


def _format_query_plan_rows(rows: list[sqlite3.Row]) -> str:
    formatted_rows: list[str] = []
    for row in rows:
        formatted_rows.append(" | ".join(str(item) for item in row))
    return "\n".join(formatted_rows)


def _explain_and_log(
    connection: sqlite3.Connection, sql: str, params: Sequence[Any] | None = None, *, label: str
) -> None:
    if not WAGGLE_LOG_QUERY_PLANS:
        return
    params_tuple = tuple(params) if params is not None else ()
    plan_sql = f"EXPLAIN QUERY PLAN {sql.strip()}"
    plan_rows = list(connection.execute(plan_sql, params_tuple))
    plan_text = _format_query_plan_rows(plan_rows)
    LOGGER.debug(
        "SQLite query plan (%s): sql=%s params=%s\n%s",
        label,
        sql.strip(),
        params_tuple,
        plan_text,
    )
