#!/usr/bin/env python3
"""
Generate examples/demo.abhi — a curated demo graph for waggle-mcp.

Theme: a fictional team (Acme Web Co.) building a web app.
Covers: 3 decisions (with reasons), 2 contradictions, 3 preferences, facts, notes.
One decision is superseded by a later decision (PostgreSQL → SQLite for dev, then back).

Re-run to regenerate:
    python3 examples/generate_demo_abhi.py

The script writes to examples/demo.abhi relative to the repo root.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

# Ensure src/ is on the path when run from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waggle.embeddings import EmbeddingModel
from waggle.graph import MemoryGraph
from waggle.models import NodeType, RelationType

OUTPUT_PATH = REPO_ROOT / "examples" / "demo.abhi"


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Use a fixed temp directory that persists during generation
    tmp_dir = Path(tempfile.mkdtemp(prefix="waggle-demo-gen-"))
    try:
        db_path = tmp_dir / "demo-gen.db"
        model = EmbeddingModel("deterministic")
        # Disable dedup so all demo nodes are inserted as distinct entries.
        # The deterministic embedding model can produce similar vectors for
        # thematically related nodes, which would cause unwanted merges.
        graph = MemoryGraph(str(db_path), model, tenant_id="local-default", enable_dedup=False)

        project = "acme-webapp"

        # Use a single connection for all operations to ensure nodes are visible to edges
        with graph._lock, graph._connect() as conn:
            # ── Add all nodes first ───────────────────────────────────────────────
            # Decision 1: Use PostgreSQL (later contradicted)
            graph.add_node(
                connection=conn,
                node_id="demo-node-0001",
                label="Database: PostgreSQL",
                content=(
                    "We decided to use PostgreSQL as the primary database for the Acme web app. "
                    "PostgreSQL offers ACID compliance, rich JSON support, and scales well for our expected load."
                ),
                node_type=NodeType.DECISION,
                tags=["database", "infrastructure", "backend"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0002",
                label="Reason: PostgreSQL chosen for ACID and JSON",
                content=(
                    "PostgreSQL was chosen because the team has prior experience with it, "
                    "it supports JSONB for flexible schema evolution, and the managed RDS offering "
                    "fits our AWS deployment plan."
                ),
                node_type=NodeType.NOTE,
                tags=["database", "reason"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0003",
                label="Database: SQLite for local dev",
                content=(
                    "For local development, we switched to SQLite to eliminate the Docker dependency "
                    "and speed up onboarding. Production still targets PostgreSQL."
                ),
                node_type=NodeType.DECISION,
                tags=["database", "development", "onboarding"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0004",
                label="Reason: SQLite reduces dev friction",
                content=(
                    "New engineers were spending 30+ minutes setting up Postgres locally. "
                    "SQLite requires zero setup and the ORM abstracts the difference."
                ),
                node_type=NodeType.NOTE,
                tags=["database", "reason", "developer-experience"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0005",
                label="Database: PostgreSQL everywhere via Docker Compose",
                content=(
                    "Final decision: use PostgreSQL in all environments (dev, staging, prod) "
                    "via Docker Compose. The SQLite shortcut caused subtle migration drift. "
                    "We added a one-command `make dev-up` to remove the setup friction."
                ),
                node_type=NodeType.DECISION,
                tags=["database", "infrastructure", "docker"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0006",
                label="Reason: parity between dev and prod prevents migration drift",
                content=(
                    "SQLite and PostgreSQL handle NULL semantics, JSON operators, and "
                    "transaction isolation differently. Two bugs in staging traced back to "
                    "SQLite-only dev. Docker Compose with a health-check solves onboarding "
                    "without sacrificing parity."
                ),
                node_type=NodeType.NOTE,
                tags=["database", "reason", "reliability"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0007",
                label="Auth: use Auth0 for SSO",
                content=(
                    "We will use Auth0 for authentication and SSO. "
                    "Rolling our own OAuth is out of scope for v1. Auth0 supports SAML for enterprise customers."
                ),
                node_type=NodeType.DECISION,
                tags=["auth", "security", "saas"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0008",
                label="Reason: Auth0 reduces security risk and time-to-market",
                content=(
                    "The team has no dedicated security engineer. Auth0 handles MFA, breach detection, "
                    "and compliance certifications. Estimated 3-week saving vs. building in-house."
                ),
                node_type=NodeType.NOTE,
                tags=["auth", "reason", "security"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0009",
                label="Deploy: AWS ECS with Fargate",
                content=(
                    "We will deploy the web app on AWS ECS using Fargate. "
                    "No EC2 instance management, auto-scaling, and integrates with our existing AWS account."
                ),
                node_type=NodeType.DECISION,
                tags=["deployment", "aws", "infrastructure"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0010",
                label="Reason: Fargate removes ops overhead for a small team",
                content=(
                    "The team is 4 engineers. Fargate means no patching, no AMI management, "
                    "and cost scales to zero when idle. Kubernetes was considered but deemed over-engineered for v1."
                ),
                node_type=NodeType.NOTE,
                tags=["deployment", "reason", "ops"],
                project=project,
            )

            # Preferences
            graph.add_node(
                connection=conn,
                node_id="demo-node-0011",
                label="Preference: TypeScript for all frontend code",
                content=(
                    "The team prefers TypeScript over plain JavaScript for all frontend work. "
                    "Strict mode enabled. No `any` without a comment explaining why."
                ),
                node_type=NodeType.PREFERENCE,
                tags=["frontend", "typescript", "code-style"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0012",
                label="Preference: dark mode as default UI theme",
                content=(
                    "The team prefers dark mode as the default UI theme. "
                    "Light mode should be available as a toggle but dark is the out-of-box experience."
                ),
                node_type=NodeType.PREFERENCE,
                tags=["ui", "design", "accessibility"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0013",
                label="Preference: small, focused pull requests",
                content=(
                    "The team strongly prefers small, focused PRs — ideally under 400 lines. "
                    "Large PRs block review and increase merge conflict risk. "
                    "Feature flags are the preferred mechanism for shipping incomplete features."
                ),
                node_type=NodeType.PREFERENCE,
                tags=["process", "code-review", "git"],
                project=project,
            )

            # Facts and notes
            graph.add_node(
                connection=conn,
                node_id="demo-node-0014",
                label="Team size: 4 engineers",
                content=(
                    "The Acme web app team has 4 engineers: 2 full-stack, 1 backend, 1 frontend/design. "
                    "No dedicated DevOps or security engineer."
                ),
                node_type=NodeType.FACT,
                tags=["team", "org"],
                project=project,
            )

            graph.add_node(
                connection=conn,
                node_id="demo-node-0015",
                label="Target launch: Q3 this year",
                content=(
                    "The target public launch date is end of Q3. "
                    "The v1 scope is intentionally narrow: auth, core CRUD, and basic reporting. "
                    "v2 will add integrations and advanced analytics."
                ),
                node_type=NodeType.NOTE,
                tags=["timeline", "scope", "planning"],
                project=project,
            )

            # ── Add all edges after all nodes exist ───────────────────────────────

            # Decision 1 edges
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0001",
                source_id="demo-node-0001",
                target_id="demo-node-0002",
                relationship=RelationType.DERIVED_FROM,
            )

            # Decision 2 edges
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0002",
                source_id="demo-node-0003",
                target_id="demo-node-0004",
                relationship=RelationType.DERIVED_FROM,
            )

            # Contradiction: SQLite decision contradicts the original PostgreSQL decision
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0003",
                source_id="demo-node-0003",
                target_id="demo-node-0001",
                relationship=RelationType.CONTRADICTS,
            )

            # Decision 3 edges
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0004",
                source_id="demo-node-0005",
                target_id="demo-node-0003",
                relationship=RelationType.UPDATES,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0005",
                source_id="demo-node-0005",
                target_id="demo-node-0006",
                relationship=RelationType.DERIVED_FROM,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0015",
                source_id="demo-node-0005",
                target_id="demo-node-0001",
                relationship=RelationType.RELATES_TO,
            )

            # Auth edges
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0006",
                source_id="demo-node-0007",
                target_id="demo-node-0008",
                relationship=RelationType.DERIVED_FROM,
            )

            # Deployment edges
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0007",
                source_id="demo-node-0009",
                target_id="demo-node-0010",
                relationship=RelationType.DERIVED_FROM,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0008",
                source_id="demo-node-0007",
                target_id="demo-node-0009",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0013",
                source_id="demo-node-0005",
                target_id="demo-node-0009",
                relationship=RelationType.RELATES_TO,
            )

            # Preference edges
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0009",
                source_id="demo-node-0011",
                target_id="demo-node-0007",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0014",
                source_id="demo-node-0012",
                target_id="demo-node-0011",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0012",
                source_id="demo-node-0013",
                target_id="demo-node-0014",
                relationship=RelationType.RELATES_TO,
            )

            # Fact/Note edges
            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0010",
                source_id="demo-node-0014",
                target_id="demo-node-0009",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0011",
                source_id="demo-node-0015",
                target_id="demo-node-0009",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0016",
                source_id="demo-node-0014",
                target_id="demo-node-0007",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0017",
                source_id="demo-node-0015",
                target_id="demo-node-0007",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0018",
                source_id="demo-node-0011",
                target_id="demo-node-0009",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0019",
                source_id="demo-node-0003",
                target_id="demo-node-0014",
                relationship=RelationType.RELATES_TO,
            )

            graph.add_edge(
                connection=conn,
                edge_id="demo-edge-0020",
                source_id="demo-node-0007",
                target_id="demo-node-0011",
                relationship=RelationType.RELATES_TO,
            )

        # ── Export ────────────────────────────────────────────────────────────
        result = graph.export_abhi(
            output_path=OUTPUT_PATH,
            include_embeddings=False,
        )
        print(f"Generated {OUTPUT_PATH}")
        print(f"  nodes: {result.node_count}, edges: {result.edge_count}")
    finally:
        # Clean up temp directory
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
