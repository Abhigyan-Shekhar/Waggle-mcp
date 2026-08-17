# Contributing

Thank you for improving Waggle Context Handoff.

1. Fork the eventual public Action repository and create a focused branch.
2. Keep event parsing in the `waggle-mcp ingest-github-event` package API; do not duplicate it in shell or the Action runner.
3. Add deterministic fixtures and tests for behavior changes. Use only synthetic data and marker secrets.
4. Run `python -m pytest tests -q`, `ruff check scripts tests`, `mypy scripts/run_action.py`, and `bash -n tests/assert_no_secret.sh`.
5. Open a pull request describing security impact, CLI compatibility, and verification output.

Contributions must preserve the offline trust boundary: no hosted Waggle dependency, external LLM call, event-text execution, repository mutation, issue comment, or permission expansion. By submitting a contribution, you agree it is licensed under Apache-2.0.
