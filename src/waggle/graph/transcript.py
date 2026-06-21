from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from waggle.evidence import build_observation_evidence
from waggle.intelligence import (
    TYPED_EDGE_CONFIDENCE,
    infer_relationship,
    normalize_text,
    tokenize_text,
)
from waggle.locks import ProcessLock
from waggle.models import (
    Node,
    NodeType,
    ObservationResult,
    RelationType,
    ReplayHit,
    TranscriptIngestionInput,
    TranscriptIngestionResult,
    TranscriptMessage,
    TranscriptRecord,
    utc_now,
)

from .base import (
    MemoryGraphBase,
    _decode_metadata,
    _encode_metadata,
    _normalized_content_hash,
    _parse_datetime,
)


def extract_conversation_candidates(user_message: str, assistant_response: str) -> list[dict[str, Any]]:
    import sys

    graph_mod = sys.modules.get("waggle.graph")
    func = None
    if graph_mod and hasattr(graph_mod, "extract_conversation_candidates"):
        f = graph_mod.extract_conversation_candidates
        if f is not extract_conversation_candidates:
            func = f
    if func is None:
        from waggle.intelligence import extract_conversation_candidates as original_func

        func = original_func
    return func(user_message=user_message, assistant_response=assistant_response)


class TranscriptMixin(MemoryGraphBase):
    """Mixin class for MemoryGraph handling transcript logging and conversation turns."""

    def _apply_observation_candidates(
        self,
        *,
        candidates: list[dict[str, Any]],
        transcript: str,
        source_turn_pair_id: str,
        user_turn_index: int,
        assistant_turn_index: int,
        observed_at: datetime,
        session_id: str,
        agent_id: str,
        project: str,
        edge_origin: str = "observe_conversation",
        connection: sqlite3.Connection | None = None,
    ) -> ObservationResult:
        """Shared extraction helper used by both observe_conversation and ingest_transcript_handoff.

        Takes pre-extracted candidates and stores them as nodes with evidence,
        then links decision->rationale edges.  Both single-turn and batch paths call this
        so memory semantics stay aligned.
        """
        result = ObservationResult()
        stored_candidate_records: list[tuple[Node, list[str]]] = []
        _candidate_texts = [str(c["content"]) for c in candidates]
        _batch_embeddings: np.ndarray | None = None
        if _candidate_texts:
            try:
                _batch_embeddings = self.embedding_model.embed_batch(_candidate_texts)
            except Exception:
                _batch_embeddings = None

            if _batch_embeddings is not None and len(_batch_embeddings) != len(_candidate_texts):
                raise ValueError(
                    f"embed_batch returned {len(_batch_embeddings)} vectors, expected {len(_candidate_texts)}"
                )

        for _idx, candidate in enumerate(candidates):
            candidate_tags = list(candidate.get("tags", []))
            speaker_tag = next((tag for tag in candidate_tags if str(tag).startswith("speaker:")), "")
            speaker = speaker_tag.split(":", 1)[1] if ":" in speaker_tag else "user"
            turn_index = user_turn_index if speaker == "user" else assistant_turn_index
            evidence = build_observation_evidence(
                transcript=transcript,
                source_text=str(candidate["content"]),
                speaker=speaker,
                turn_index=turn_index,
                observed_at=observed_at,
                session_id=session_id,
            )
            # Pass the pre-computed vector when available; otherwise let add_node
            # fall back to its own embed() call (preserves backward-compatibility).
            _precomputed: np.ndarray | None = (
                _batch_embeddings[_idx] if _batch_embeddings is not None and _idx < len(_batch_embeddings) else None
            )
            store_result = self.add_node(
                label=str(candidate["label"]),
                content=str(candidate["content"]),
                node_type=candidate["node_type"],
                tags=candidate_tags,
                source_prompt=transcript,
                source_turn_pair_id=source_turn_pair_id,
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                evidence_records=[evidence],
                valid_from=observed_at,
                embedding=_precomputed,
                connection=connection,
            )
            result.stored_nodes.append(store_result.node)
            stored_candidate_records.append((store_result.node, candidate_tags))
            if store_result.created:
                result.created_count += 1
            else:
                result.reused_count += 1
            for conflict in store_result.conflicts:
                if conflict.other_node_id not in {item.other_node_id for item in result.conflicts}:
                    result.conflicts.append(conflict)

        decision_nodes = [
            (node, tags) for node, tags in stored_candidate_records if node.node_type == NodeType.DECISION
        ]
        rationale_nodes = [
            (node, tags)
            for node, tags in stored_candidate_records
            if "decision-rationale" in tags and node.node_type == NodeType.FACT
        ]
        inferred_edges_count = 0
        for decision_node, decision_tags in decision_nodes:
            decision_categories = {
                tag
                for tag in decision_tags
                if tag in {"database", "backend-framework", "frontend-framework", "auth-mechanism", "api-style"}
            }
            for rationale_node, rationale_tags in rationale_nodes:
                rationale_categories = {
                    tag
                    for tag in rationale_tags
                    if tag in {"database", "backend-framework", "frontend-framework", "auth-mechanism", "api-style"}
                }
                if rationale_categories and decision_categories and not (rationale_categories & decision_categories):
                    continue
                self.add_edge(
                    source_id=decision_node.id,
                    target_id=rationale_node.id,
                    relationship=RelationType.DEPENDS_ON,
                    metadata={"origin": edge_origin},
                    connection=connection,
                )
                inferred_edges_count += 1
        neighbor_edges_count = self._link_observation_candidate_neighbors(
            stored_candidate_records=stored_candidate_records,
            edge_origin=edge_origin,
            connection=connection,
        )
        result.edges_inferred = inferred_edges_count + neighbor_edges_count
        return result

    def _link_observation_candidate_neighbors(
        self,
        *,
        stored_candidate_records: list[tuple[Node, list[str]]],
        edge_origin: str,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        if len(stored_candidate_records) < 2:
            return 0

        category_tags = {"database", "backend-framework", "frontend-framework", "auth-mechanism", "api-style"}
        created_pairs: set[tuple[str, str, str]] = set()

        for index, (source_node, source_tags) in enumerate(stored_candidate_records):
            source_text = normalize_text(f"{source_node.label} {source_node.content}")
            source_categories = {tag for tag in source_tags if tag in category_tags}
            source_tokens = tokenize_text(source_node.content)
            for target_node, target_tags in stored_candidate_records[index + 1 :]:
                if source_node.id == target_node.id:
                    continue

                target_text = normalize_text(f"{target_node.label} {target_node.content}")
                target_categories = {tag for tag in target_tags if tag in category_tags}
                target_tokens = tokenize_text(target_node.content)

                edge_specs: list[tuple[str, str, RelationType, str, float]] = []
                if target_node.node_type == NodeType.ENTITY and normalize_text(target_node.label) in source_text:
                    edge_specs.append(
                        (
                            source_node.id,
                            target_node.id,
                            RelationType.RELATES_TO,
                            "entity-mention",
                            TYPED_EDGE_CONFIDENCE,
                        )
                    )
                if source_node.node_type == NodeType.ENTITY and normalize_text(source_node.label) in target_text:
                    edge_specs.append(
                        (
                            target_node.id,
                            source_node.id,
                            RelationType.RELATES_TO,
                            "entity-mention",
                            TYPED_EDGE_CONFIDENCE,
                        )
                    )

                shared_tokens = source_tokens & target_tokens
                has_shared_category = bool(source_categories & target_categories)
                if (
                    not edge_specs
                    and source_node.node_type != NodeType.ENTITY
                    and target_node.node_type != NodeType.ENTITY
                ):
                    if len(shared_tokens) >= 2 or has_shared_category:
                        inferred = infer_relationship(
                            source_node,
                            target_node,
                            shared_tokens=shared_tokens,
                            cosine_similarity=self._node_cosine_similarity(source_node, target_node),
                        )
                        if inferred is not None:
                            rel_type, confidence = inferred
                            reason = (
                                "shared-category" if has_shared_category and len(shared_tokens) < 2 else "shared-tokens"
                            )
                            edge_specs.append((source_node.id, target_node.id, rel_type, reason, confidence))

                for from_id, to_id, relationship, reason, confidence in edge_specs:
                    key = (from_id, to_id, relationship.value)
                    if key in created_pairs:
                        continue
                    self.add_edge(
                        source_id=from_id,
                        target_id=to_id,
                        relationship=relationship,
                        metadata={"origin": edge_origin, "inferred": reason, "edge_confidence": confidence},
                        connection=connection,
                    )
                    created_pairs.add(key)
        return len(created_pairs)

    def observe_conversation(
        self,
        *,
        user_message: str,
        assistant_response: str,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> ObservationResult:
        """Observe a completed user-assistant turn with verbatim-first persistence.

        Follows new architecture:
        1. PERSIST verbatim turn first (mandatory). If this fails, the call fails.
        2. RUN extraction in try/except. If it raises, log and continue (non-fatal).
        3. RETURN structured result with turn_id, verbatim_stored, nodes_extracted, edges_inferred, extraction_errors.

        Uses ProcessLock to protect multi-statement transaction from concurrent access.
        """
        logger = logging.getLogger(__name__)
        transcript = f"user: {user_message.strip()}\nassistant: {assistant_response.strip()}".strip()
        observed_at = utc_now()
        turn_pair_id = str(uuid4())

        result = ObservationResult(
            turn_id=turn_pair_id,
            verbatim_stored=False,
            nodes_extracted=0,
            edges_inferred=0,
            extraction_errors=[],
        )

        # Wrap multi-statement operations in cross-process lock
        lock_path = str(self.db_path) + ".lock"
        with ProcessLock(lock_path):
            # ===== STEP 1: PERSIST VERBATIM TURN (MANDATORY) =====
            with self._lock, self._pool.checkout() as connection:
                next_turn_index = self._next_transcript_turn_index(
                    connection,
                    session_id=session_id,
                    project=project,
                    agent_id=agent_id,
                )
                turns = [
                    ("user", user_message.strip(), next_turn_index),
                    ("assistant", assistant_response.strip(), next_turn_index + 1),
                ]
                try:
                    for role, text, turn_index in turns:
                        if not text:
                            continue
                        self._store_transcript_record(
                            connection,
                            agent_id=agent_id,
                            project=project,
                            session_id=session_id,
                            observed_at=observed_at,
                            turn_index=turn_index,
                            role=role,
                            transcript_text=text,
                            turn_pair_id=turn_pair_id,
                        )
                    result.verbatim_stored = True
                    connection.commit()
                except Exception as verbatim_err:
                    # Verbatim persistence is mandatory. If it fails, the entire call fails.
                    connection.rollback()
                    logger.exception(
                        "Failed to persist verbatim turn %s: %s",
                        turn_pair_id,
                        verbatim_err,
                    )
                    raise
                # ===== STEP 2: RUN EXTRACTION IN TRY/EXCEPT (NON-BLOCKING) =====
                extraction_candidates = []
                try:
                    extraction_candidates = extract_conversation_candidates(
                        user_message=user_message,
                        assistant_response=assistant_response,
                    )
                except Exception as extraction_err:
                    logger.exception(
                        "Extraction failed for turn %s: %s",
                        turn_pair_id,
                        extraction_err,
                    )
                    result.extraction_errors.append(
                        f"Extraction exception: {type(extraction_err).__name__}: {extraction_err!s}"
                    )
                    # Continue: verbatim is stored, extraction is optional enrichment

                # ===== STEP 3: APPLY EXTRACTED CANDIDATES (IF ANY) =====
                if extraction_candidates:
                    try:
                        connection.execute("SAVEPOINT apply_candidates")
                        try:
                            candidates_result = self._apply_observation_candidates(
                                candidates=extraction_candidates,
                                transcript=transcript,
                                source_turn_pair_id=turn_pair_id,
                                user_turn_index=next_turn_index,
                                assistant_turn_index=next_turn_index + 1,
                                observed_at=observed_at,
                                session_id=session_id,
                                agent_id=agent_id,
                                project=project,
                                connection=connection,
                            )
                            connection.execute("RELEASE SAVEPOINT apply_candidates")
                        except Exception as e:
                            connection.execute("ROLLBACK TO SAVEPOINT apply_candidates")
                            raise e
                        # Merge extraction results into main result
                        result.stored_nodes = candidates_result.stored_nodes
                        result.created_count = candidates_result.created_count
                        result.reused_count = candidates_result.reused_count
                        result.conflicts = candidates_result.conflicts
                        result.nodes_extracted = candidates_result.created_count
                        result.edges_inferred = candidates_result.edges_inferred
                    except Exception as candidate_err:
                        logger.exception(
                            "Candidate application failed for turn %s: %s",
                            turn_pair_id,
                            candidate_err,
                        )
                        result.extraction_errors.append(
                            f"Candidate storage exception: {type(candidate_err).__name__}: {candidate_err!s}"
                        )
                        # Continue: verbatim persists regardless

                # ===== STEP 4: WINDOW CONTEXT AND EDGES (SAME AS BEFORE) =====
                try:
                    repo_id, window_id = self.resolve_window_context(
                        project=project, session_id=session_id, connection=connection
                    )
                    self._update_window_node_count(connection, window_id)
                    self._mark_window_embedding_stale(connection, window_id)
                except Exception as window_err:
                    logger.warning(
                        "Window context update failed for turn %s: %s",
                        turn_pair_id,
                        window_err,
                    )
                    result.extraction_errors.append(f"Window context error: {window_err!s}")
                    window_id = ""
                    repo_id = ""
            # Derive edges outside the transaction lock
            if window_id and repo_id:
                try:
                    self.derive_context_window_edges(window_id, repo_id)
                except Exception as edge_err:
                    logger.warning(
                        "Context window edge derivation failed for turn %s: %s",
                        turn_pair_id,
                        edge_err,
                    )
                    result.extraction_errors.append(f"Edge derivation error: {edge_err!s}")

        return result

    @staticmethod
    def _message_fingerprint(msg: TranscriptMessage, raw_position: int) -> str:
        """Compute a stable dedup identity for a transcript message.

        If the message supplies a client-side ``message_id``, use it directly.
        Otherwise compute a deterministic positional fingerprint from
        (role, content, raw_position, timestamp-or-empty).

        Positional fingerprints are idempotent only for identical reruns.
        Prepending, removing, or reordering messages in a partial resubmit
        will produce different fingerprints and be treated as new input.
        This is a documented v1 limitation.
        """
        if msg.message_id:
            return msg.message_id
        payload = "\x00".join(
            [
                msg.role,
                msg.content,
                str(raw_position),
                msg.timestamp or "",
            ]
        )
        return "fp:" + hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _build_extractive_blocks(
        messages: list[TranscriptMessage],
    ) -> list[tuple[str, str]]:
        """Collapse consecutive same-role extractive (user/assistant) messages into blocks.

        system and tool messages are skipped for block formation but do not
        split or interrupt blocks.  This is the v1 rule; see docs/backlog for
        the tool_boundary_splits_blocks refinement.

        Returns a list of (role, joined_content) tuples.
        """
        blocks: list[tuple[str, str]] = []
        for msg in messages:
            if msg.role not in ("user", "assistant"):
                # system/tool: skip for block purposes, no split
                continue
            if blocks and blocks[-1][0] == msg.role:
                # Collapse consecutive same-role messages
                blocks[-1] = (blocks[-1][0], blocks[-1][1] + "\n\n" + msg.content)
            else:
                blocks.append((msg.role, msg.content))
        return blocks

    @staticmethod
    def _build_session_extractive_blocks(
        rows: list[Any],
        newly_written_identities: set[str],
    ) -> list[tuple[str, str, int, bool]]:
        """Build extractive blocks from the full ordered session transcript (from DB rows).

        Each block is (role, joined_content, first_turn_index, has_new_message).
        - role: 'user' or 'assistant' (system/tool rows are skipped).
        - joined_content: consecutive same-role messages joined with '\n\n'.
        - first_turn_index: the turn_index of the first row that contributed to this block.
        - has_new_message: True if ANY message in this block was newly written this run.

        This is the correct block-scan surface for extraction: it sees the full
        session history so a previously-unpaired trailing user can be completed by
        a newly-arrived assistant message in the next ingestion call.
        """
        blocks: list[tuple[str, str, int, bool]] = []
        for row in rows:
            role: str = row["role"]
            if role not in ("user", "assistant"):
                continue
            content: str = row["transcript_text"]
            turn_index: int = row["turn_index"]
            identity: str | None = row["message_identity"]
            is_new = identity in newly_written_identities if identity else False
            if blocks and blocks[-1][0] == role:
                prev_role, prev_content, prev_turn, prev_new = blocks[-1]
                blocks[-1] = (prev_role, prev_content + "\n\n" + content, prev_turn, prev_new or is_new)
            else:
                blocks.append((role, content, turn_index, is_new))
        return blocks

    def ingest_transcript_handoff(
        self,
        payload: TranscriptIngestionInput,
        *,
        export_format: str = "both",
        output_path: str | None = None,
        max_nodes: int = 25,
    ) -> TranscriptIngestionResult:
        """Batch-ingest a full ordered transcript, extract durable memory from logical turns,
        and optionally export a session-scoped handoff bundle.

        Supported backend: SQLite only in v1.  Neo4j support is deferred.

        Algorithm (block-windowing):
        1. Persist every message to transcript_records with dedup via message_identity.
        2. Build an extractive stream keeping only user/assistant messages.
        3. Collapse consecutive same-role extractive messages into one block.
        4. Scan collapsed blocks left to right:
           - user -> assistant   => one logical turn (extract from both).
           - leading assistant   => transcript-only, skipped for extraction.
           - trailing user       => transcript-only, counted as unpaired.
           - After consuming a u->a pair, continue from the next remaining block.

        Tool-interleaving behavior (v1 simplification, documented):
        - user -> tool -> tool -> assistant  =>  one logical turn: user -> assistant.
        - user -> assistant -> tool -> tool -> assistant => user -> (assistant + assistant).
        Tool boundary splitting is a planned v2 refinement.
        """
        result = TranscriptIngestionResult(
            project=payload.project,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
        )

        if not payload.messages:
            result.export_skipped = True
            result.export_skipped_reason = "no_messages"
            return result

        result.input_message_count = len(payload.messages)
        observed_at = utc_now()

        # Step 2: Persist all messages; collect identities of newly written ones.
        # Use ProcessLock to protect batch insert from concurrent access
        lock_path = str(self.db_path) + ".lock"
        with ProcessLock(lock_path):
            newly_written_identities: set[str] = set()
            with self._lock, self._pool.checkout() as connection:
                base_turn_index = self._next_transcript_turn_index(
                    connection,
                    session_id=payload.session_id,
                    project=payload.project,
                    agent_id=payload.agent_id,
                )
                for raw_pos, msg in enumerate(payload.messages):
                    identity = self._message_fingerprint(msg, raw_pos)
                    written = self._store_transcript_record(
                        connection,
                        agent_id=payload.agent_id,
                        project=payload.project,
                        session_id=payload.session_id,
                        observed_at=observed_at,
                        turn_index=base_turn_index + raw_pos,
                        role=msg.role,
                        transcript_text=msg.content,
                        message_identity=identity,
                    )
                    if written:
                        result.transcript_records_written += 1
                        newly_written_identities.add(identity)
                    else:
                        result.transcript_records_skipped += 1

            # Step 3: Load the FULL session transcript from the DB ordered by turn_index.
            # We must scan the full session — not just newly written messages — so that a
            # previously-unpaired trailing user block can be paired with an assistant that
            # arrives in a later ingestion call.
            with self._lock, self._pool.checkout() as connection:
                session_rows = connection.execute(
                    """
                    SELECT role, transcript_text, turn_index, message_identity
                    FROM transcript_records
                    WHERE tenant_id = ? AND session_id = ? AND project = ? AND agent_id = ?
                    ORDER BY turn_index ASC, id ASC
                    """,
                    (self.tenant_id, payload.session_id, payload.project, payload.agent_id),
                ).fetchall()

            # Step 4: Build session-scoped extractive blocks, each tagged with
            # has_new_message=True iff any row in that block was newly written this run.
            # (role, joined_content, first_turn_index, has_new_message)
            session_blocks = self._build_session_extractive_blocks(session_rows, newly_written_identities)

            # Step 4.5: Fetch already extracted turn indices for this session/project/agent
            extracted_turn_indices = set()
            with self._lock, self._pool.checkout() as connection:
                node_filters = ["tenant_id = ?"]
                node_params = [self.tenant_id]
                if payload.session_id:
                    node_filters.append("session_id = ?")
                    node_params.append(payload.session_id)
                if payload.project:
                    node_filters.append("project = ?")
                    node_params.append(payload.project)
                if payload.agent_id:
                    node_filters.append("agent_id = ?")
                    node_params.append(payload.agent_id)
                rows = connection.execute(
                    f"SELECT evidence_records FROM nodes WHERE {' AND '.join(node_filters)}", tuple(node_params)
                ).fetchall()
                for r_row in rows:
                    if r_row["evidence_records"]:
                        try:
                            records = json.loads(r_row["evidence_records"])
                            for r in records:
                                if isinstance(r, dict) and "turn_index" in r:
                                    extracted_turn_indices.add(r["turn_index"])
                        except Exception:
                            pass

            # Determine if we have any turns to extract (only relevant if transcript_records_written == 0)
            any_turns_to_extract = False
            if result.transcript_records_written == 0:
                i = 0
                while i < len(session_blocks):
                    role, content, role_turn_index, block_has_new = session_blocks[i]
                    if role == "assistant":
                        i += 1
                        continue
                    if i + 1 < len(session_blocks) and session_blocks[i + 1][0] == "assistant":
                        user_turn_index = role_turn_index
                        assistant_turn_index = session_blocks[i + 1][2]
                        user_has_new = block_has_new
                        asst_has_new = session_blocks[i + 1][3]

                        needs_extraction = (user_has_new or asst_has_new) or (
                            user_turn_index not in extracted_turn_indices
                            and assistant_turn_index not in extracted_turn_indices
                        )
                        if needs_extraction:
                            any_turns_to_extract = True
                            break
                        i += 2
                    else:
                        i += 1

            # If every message was a duplicate (full re-run) and there are no turns to extract, skip extraction.
            if result.transcript_records_written == 0 and not any_turns_to_extract:
                result.export_skipped = True
                result.export_skipped_reason = "all_messages_already_ingested"
                # Still produce an export bundle from existing session memory.
                _export = self._maybe_export_bundle(
                    payload=payload,
                    export_format=export_format,
                    output_path=output_path,
                    max_nodes=max_nodes,
                )
                if _export is not None:
                    result.export_skipped = False
                    result.markdown_path = _export.get("markdown_path")
                    result.json_path = _export.get("json_path")
                    result.export_node_count = _export.get("node_count", 0)
                    result.export_edge_count = _export.get("edge_count", 0)
                checkpoint = self._export_transcript_handoff_checkpoint(
                    payload=payload,
                    output_path=output_path,
                )
                result.checkpoint_path = checkpoint.get("checkpoint_path")
                result.checkpoint_scope = checkpoint.get("checkpoint_scope", "")
                return result

            # Step 5: Scan blocks left to right; only extract turns where they either have new messages
            # or haven't been extracted yet.
            i = 0
            while i < len(session_blocks):
                role, content, role_turn_index, block_has_new = session_blocks[i]
                if role == "assistant":
                    # Leading or orphaned assistant: transcript-only, skip.
                    i += 1
                    continue
                # role == "user"
                if i + 1 < len(session_blocks) and session_blocks[i + 1][0] == "assistant":
                    user_content = content
                    user_turn_index = role_turn_index
                    user_has_new = block_has_new
                    assistant_content = session_blocks[i + 1][1]
                    assistant_turn_index = session_blocks[i + 1][2]
                    asst_has_new = session_blocks[i + 1][3]

                    if result.transcript_records_written > 0:
                        needs_extraction = user_has_new or asst_has_new
                    else:
                        needs_extraction = (
                            user_turn_index not in extracted_turn_indices
                            and assistant_turn_index not in extracted_turn_indices
                        )
                    if needs_extraction:
                        transcript = f"user: {user_content}\nassistant: {assistant_content}"
                        candidates = extract_conversation_candidates(
                            user_message=user_content,
                            assistant_response=assistant_content,
                        )
                        with self._lock, self._pool.checkout() as connection:
                            connection.execute("SAVEPOINT apply_candidates")
                            try:
                                turn_result = self._apply_observation_candidates(
                                    candidates=candidates,
                                    transcript=transcript,
                                    source_turn_pair_id=str(uuid4()),
                                    user_turn_index=user_turn_index,
                                    assistant_turn_index=assistant_turn_index,
                                    observed_at=observed_at,
                                    session_id=payload.session_id,
                                    agent_id=payload.agent_id,
                                    project=payload.project,
                                    edge_origin="ingest_transcript_handoff",
                                    connection=connection,
                                )
                                connection.execute("RELEASE SAVEPOINT apply_candidates")
                            except Exception as e:
                                connection.execute("ROLLBACK TO SAVEPOINT apply_candidates")
                                raise e
                        result.logical_turns_processed += 1
                        result.nodes_created += turn_result.created_count
                        result.nodes_reused += turn_result.reused_count
                        result.conflicts += len(turn_result.conflicts)
                    i += 2
                else:
                    # Trailing user block with no following assistant: transcript-only.
                    result.unpaired_trailing_blocks += 1
                    i += 1

            # Step 5: Export a session-scoped prime bundle.
            _export = self._maybe_export_bundle(
                payload=payload,
                export_format=export_format,
                output_path=output_path,
                max_nodes=max_nodes,
            )
            if _export is not None:
                result.markdown_path = _export.get("markdown_path")
                result.json_path = _export.get("json_path")
                result.export_node_count = _export.get("node_count", 0)
                result.export_edge_count = _export.get("edge_count", 0)
            else:
                result.export_skipped = True
                result.export_skipped_reason = "no_nodes_in_session"
            checkpoint = self._export_transcript_handoff_checkpoint(
                payload=payload,
                output_path=output_path,
            )
            result.checkpoint_path = checkpoint.get("checkpoint_path")
            result.checkpoint_scope = checkpoint.get("checkpoint_scope", "")
        return result

    def _maybe_export_bundle(
        self,
        *,
        payload: TranscriptIngestionInput,
        export_format: str,
        output_path: str | None,
        max_nodes: int,
    ) -> dict[str, Any] | None:
        """Export a session-scoped context bundle after ingestion, if nodes exist."""
        stats = self.get_stats()
        if stats.total_nodes == 0:
            return None
        exported = self.export_context_bundle(
            mode="prime",
            query="",
            project=payload.project,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            max_nodes=max_nodes,
            max_depth=2,
            retrieval_mode="graph",
            format=export_format,
            output_path=output_path,
            include_edges=True,
            include_timestamps=True,
            include_source_prompt=False,
            audience="llm",
        )
        return {
            "markdown_path": exported.markdown_path,
            "json_path": exported.json_path,
            "node_count": exported.node_count,
            "edge_count": exported.edge_count,
        }

    def _export_transcript_handoff_checkpoint(
        self,
        *,
        payload: TranscriptIngestionInput,
        output_path: str | None,
    ) -> dict[str, Any]:
        checkpoint_output_path: str | None = None
        if output_path:
            checkpoint_output_path = str(Path(output_path).with_suffix(".abhi"))

        exported = self.export_abhi(
            output_path=checkpoint_output_path,
            project=payload.project,
            agent_id=payload.agent_id,
            session_id=payload.session_id,
            scope="session",
            include_embeddings=True,
        )
        return {
            "checkpoint_path": exported.output_path,
            "checkpoint_scope": "session",
        }

    def _row_to_transcript_record(self, row: sqlite3.Row) -> TranscriptRecord:
        row_keys = set(row.keys())
        return TranscriptRecord(
            id=row["id"],
            tenant_id=row["tenant_id"] if "tenant_id" in row_keys else self.tenant_id,
            agent_id=row["agent_id"] if "agent_id" in row_keys else "",
            project=row["project"] if "project" in row_keys else "",
            session_id=row["session_id"] if "session_id" in row_keys else "",
            observed_at=_parse_datetime(row["observed_at"]),
            turn_index=int(row["turn_index"] or 0),
            role=row["role"] or "",
            transcript_text=row["transcript_text"],
            embedding_model_id=row["embedding_model_id"] if "embedding_model_id" in row_keys else "",
            embedding_dim=int(row["embedding_dim"] or 0) if "embedding_dim" in row_keys else 0,
            content_hash=row["content_hash"] if "content_hash" in row_keys else "",
            turn_pair_id=row["turn_pair_id"] if "turn_pair_id" in row_keys else "",
            metadata=_decode_metadata(row["metadata"]) if "metadata" in row_keys else {},
        )

    def _transcript_scope_matches(
        self,
        record: TranscriptRecord,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> bool:
        normalized_agent = agent_id.strip().lower()
        normalized_project = project.strip().lower()
        normalized_session = session_id.strip().lower()
        if normalized_agent and record.agent_id.strip().lower() != normalized_agent:
            return False
        if normalized_project and record.project.strip().lower() != normalized_project:
            return False
        return not (normalized_session and record.session_id.strip().lower() != normalized_session)

    def list_transcript_records(
        self,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[TranscriptRecord]:
        filters = ["tenant_id = ?"]
        params: list[Any] = [self.tenant_id]
        if project.strip():
            filters.append("project = ?")
            params.append(project.strip())
        if session_id.strip():
            filters.append("session_id = ?")
            params.append(session_id.strip())
        elif agent_id.strip():
            filters.append("agent_id = ?")
            params.append(agent_id.strip())
        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                f"""
                SELECT id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role, transcript_text,
                       embedding_model_id, embedding_dim, content_hash, turn_pair_id, metadata
                FROM transcript_records
                WHERE {" AND ".join(filters)}
                ORDER BY observed_at ASC, turn_index ASC
                LIMIT ? OFFSET ?
                """,
                (*params, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [self._row_to_transcript_record(row) for row in rows]

    def count_transcript_records(
        self,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> int:
        filters = ["tenant_id = ?"]
        params: list[Any] = [self.tenant_id]
        if project.strip():
            filters.append("project = ?")
            params.append(project.strip())
        if session_id.strip():
            filters.append("session_id = ?")
            params.append(session_id.strip())
        elif agent_id.strip():
            filters.append("agent_id = ?")
            params.append(agent_id.strip())
        with self._lock, self._pool.checkout() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM transcript_records
                WHERE {" AND ".join(filters)}
                """,
                tuple(params),
            ).fetchone()
        return int(row["cnt"] or 0)

    def search_transcript_records(
        self,
        *,
        query: str,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        limit: int = 25,
    ) -> list[ReplayHit]:
        query_text = query.strip()
        if not query_text:
            return []
        return self._query_replay_hits(
            query=self._expand_query_aliases(query_text),
            max_hits=max(1, int(limit)),
            agent_id=agent_id,
            project=project,
            session_id=session_id,
        )

    def _next_transcript_turn_index(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        project: str = "",
        agent_id: str = "",
    ) -> int:
        filters = ["tenant_id = ?", "session_id = ?"]
        params = [self.tenant_id, session_id]
        if project:
            filters.append("project = ?")
            params.append(project)
        if agent_id:
            filters.append("agent_id = ?")
            params.append(agent_id)
        row = connection.execute(
            f"""
            SELECT COALESCE(MAX(turn_index), -1) AS max_turn_index
            FROM transcript_records
            WHERE {" AND ".join(filters)}
            """,
            tuple(params),
        ).fetchone()
        max_turn_index = row["max_turn_index"]
        return int(-1 if max_turn_index is None else max_turn_index) + 1

    def _store_transcript_record(
        self,
        connection: sqlite3.Connection,
        *,
        agent_id: str,
        project: str,
        session_id: str,
        observed_at: datetime,
        turn_index: int,
        role: str,
        transcript_text: str,
        turn_pair_id: str = "",
        metadata: dict[str, Any] | None = None,
        message_identity: str | None = None,
    ) -> bool:
        """Insert a transcript record.  Returns True if written, False if skipped (dedup)."""
        embedding, embedding_model_id, embedding_dim = self._embed_with_metadata(transcript_text)
        record = TranscriptRecord(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
            observed_at=observed_at,
            turn_index=turn_index,
            role=role,
            transcript_text=transcript_text,
            embedding_model_id=embedding_model_id,
            embedding_dim=embedding_dim,
            content_hash=_normalized_content_hash(transcript_text),
            turn_pair_id=turn_pair_id,
            metadata=metadata or {},
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO transcript_records (
                id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role,
                transcript_text, embedding, embedding_model_id, embedding_dim, content_hash, turn_pair_id, metadata, message_identity
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.tenant_id,
                record.agent_id,
                record.project,
                record.session_id,
                record.observed_at.isoformat(),
                record.turn_index,
                record.role,
                record.transcript_text,
                self.embedding_model.to_bytes(embedding),
                record.embedding_model_id,
                record.embedding_dim,
                record.content_hash,
                record.turn_pair_id,
                _encode_metadata(record.metadata),
                message_identity,
            ),
        )
        return cursor.rowcount > 0

    def _fetch_transcript_row(self, connection: sqlite3.Connection, transcript_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT id, tenant_id, agent_id, project, session_id, observed_at, turn_index, role, transcript_text,
                   embedding, embedding_model_id, embedding_dim, content_hash, turn_pair_id, metadata
            FROM transcript_records
            WHERE id = ? AND tenant_id = ?
            """,
            (transcript_id, self.tenant_id),
        ).fetchone()
