from __future__ import annotations

import importlib
import logging
import sqlite3


def test_explain_and_log_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("WAGGLE_LOG_QUERY_PLANS", "false")
    import waggle.query_plans as query_plans

    importlib.reload(query_plans)

    class FakeConnection:
        def execute(self, sql, params=None):
            raise AssertionError("EXPLAIN QUERY PLAN should not be executed when logging is disabled")

    query_plans._explain_and_log(FakeConnection(), "SELECT 1", (), label="test disabled")


def test_explain_and_log_runs_and_logs_plan_when_enabled(monkeypatch, caplog) -> None:
    monkeypatch.setenv("WAGGLE_LOG_QUERY_PLANS", "true")
    import waggle.query_plans as query_plans

    importlib.reload(query_plans)

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO users (name) VALUES (?)", ("alice",))

    with caplog.at_level(logging.DEBUG, logger="waggle.query_plans"):
        query_plans._explain_and_log(
            connection,
            "SELECT id, name FROM users WHERE name = ?",
            ("alice",),
            label="transcript list",
        )

    assert "SQLite query plan" in caplog.text
    assert "transcript list" in caplog.text
    assert "sql=SELECT id, name FROM users WHERE name = ?" in caplog.text
    assert "SEARCH" in caplog.text or "SCAN" in caplog.text
