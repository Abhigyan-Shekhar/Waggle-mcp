from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import warnings
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from waggle.errors import (
    ConflictResolutionError,
    DanglingEdgeError,
    HashVerificationError,
    SchemaVersionError,
    ValidationFailure,
)
from waggle.models import (
    AbhiChunkLoadResult,
    AbhiExportResult,
    AbhiInspectResult,
    AbhiMergeResult,
    AbhiQueryResult,
    AbhiValidationResult,
    EdgeDiffRecord,
    FieldDelta,
    FieldLevelDiffResult,
    MergeConflictRecord,
    MergeStrategyConfig,
    NodeDiffRecord,
)

logger = logging.getLogger(__name__)

ABHI_SPEC_VERSION = "2.0.0"
ABHI_MAJOR_VERSION = 2
ABHI_ENCRYPTION_ALGORITHM = "aes-256-gcm"

# Magic bytes prepended to every .abhi file: W G L \x01 (format version byte).
# v0 = bare ZIP (no magic, legacy files written before this was introduced)
# v1 = WGL\x01 prefix  ← current
# The reader strips these 4 bytes before passing the payload to zipfile.
# Bump the version byte (e.g. \x02) only when the ZIP layout itself changes in
# a backwards-incompatible way.
ABHI_MAGIC = b"\x57\x47\x4c\x01"  # W G L \x01
ABHI_MAGIC_LEN = len(ABHI_MAGIC)
_ABHI_ZIP_MAGIC = b"PK\x03\x04"  # standard ZIP local-file-header signature
ABHI_SIGNATURE_ALGORITHM = "ed25519"
ABHI_CHUNK_NODE_LIMIT = 64
ABHI_TRANSCRIPTS_MEMBER = "transcripts.jsonl"
ABHI_NODES_MEMBER = "nodes.jsonl"
ABHI_EDGES_MEMBER = "edges.jsonl"
ABHI_CONTEXT_WINDOWS_MEMBER = "context_windows.jsonl"
ABHI_MANIFEST_MEMBER = "manifest.json"
ABHI_SIGNATURE_MEMBER = "signatures/content.ed25519"
ABHI_PUBLIC_KEY_MEMBER = "signatures/public_key.pem"
ABHI_DETERMINISTIC_ZIP_TIMESTAMP = (2000, 1, 1, 0, 0, 0)

ABHI_MERGE_STRATEGIES = (
    "contradict",
    "last_write_wins",
    "prefer_left",
    "prefer_right",
)
DEFAULT_ABHI_MERGE_STRATEGY = "last_write_wins"


def _validate_abhi_merge_strategy(merge_strategy: str) -> str:
    """Validate and return a supported .abhi merge strategy."""
    normalized = str(merge_strategy).strip()

    if normalized not in ABHI_MERGE_STRATEGIES:
        raise ValidationFailure(
            f"Invalid merge_strategy '{merge_strategy}'. Supported strategies: {', '.join(ABHI_MERGE_STRATEGIES)}."
        )

    return normalized


DIFFED_FIELDS: frozenset[str] = frozenset(
    {
        "label",
        "content",
        "node_type",
        "tags",
        "valid_from",
        "valid_to",
        "aliases",
        "metadata",
    }
)

IGNORED_FIELDS: frozenset[str] = frozenset(
    {
        "updated_at",
        "access_count",
        "embedding_b64",
        "embedding_model_id",
        "embedding_dim",
    }
)

EDGE_DIFFED_FIELDS: frozenset[str] = frozenset(
    {
        "relationship",
        "weight",
        "source_id",
        "target_id",
        "metadata",
    }
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_with_prefix(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _record_lines(records: list[dict[str, Any]]) -> bytes:
    if not records:
        return b""
    return b"".join(_canonical_json(record) + b"\n" for record in records)


def _stable_record_sort_key(record: dict[str, Any], *fields: str) -> tuple[str, ...]:
    values = [str(record.get(field, "")) for field in fields]
    values.append(_canonical_json(record).decode("utf-8"))
    return tuple(values)


def _sorted_records(records: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
    return sorted((deepcopy(record) for record in records), key=lambda record: _stable_record_sort_key(record, *fields))


def _deterministic_zip_info(member_name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(member_name, date_time=ABHI_DETERMINISTIC_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    info.extra = b""
    return info


def _archive_writestr(archive: zipfile.ZipFile, member_name: str, payload: bytes) -> None:
    archive.writestr(_deterministic_zip_info(member_name), payload)


def _parse_lines(payload: bytes) -> list[dict[str, Any]]:
    if not payload:
        return []
    result: list[dict[str, Any]] = []
    for line in payload.decode("utf-8").splitlines():
        text = line.strip()
        if text:
            result.append(json.loads(text))
    return result


def _scope_match(record: dict[str, Any], *, project: str, agent_id: str, session_id: str) -> bool:
    if project.strip() and str(record.get("project", "")).strip() != project.strip():
        return False
    if agent_id.strip() and str(record.get("agent_id", "")).strip() != agent_id.strip():
        return False
    return not (session_id.strip() and str(record.get("session_id", "")).strip() != session_id.strip())


def _redact_text(text: str, patterns: list[str]) -> str:
    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED]", redacted)
    return redacted


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
    return kdf.derive(passphrase.encode("utf-8"))


def _encrypt_bytes(payload: bytes, *, passphrase: str) -> dict[str, Any]:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, payload, None)
    return {
        "algorithm": ABHI_ENCRYPTION_ALGORITHM,
        "kdf": "pbkdf2-hmac-sha256",
        "iterations": 600_000,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


def _decrypt_bytes(payload: dict[str, Any], *, passphrase: str) -> bytes:
    if not passphrase:
        raise ValidationFailure("This .abhi file is encrypted. Provide a passphrase.")
    salt = base64.b64decode(str(payload["salt_b64"]))
    nonce = base64.b64decode(str(payload["nonce_b64"]))
    ciphertext = base64.b64decode(str(payload["ciphertext_b64"]))
    key = _derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValidationFailure("Could not decrypt .abhi payload. Check the passphrase.") from exc


def _read_member(archive: zipfile.ZipFile, manifest: dict[str, Any], member_name: str, *, passphrase: str) -> bytes:
    metadata = dict(manifest.get("members", {}).get(member_name, {}))
    if member_name not in archive.namelist():
        return b""
    raw = archive.read(member_name)
    if metadata.get("encrypted"):
        payload = json.loads(raw.decode("utf-8"))
        return _decrypt_bytes(payload, passphrase=passphrase)
    return raw


def _write_member(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    member_name: str,
    payload: bytes,
    *,
    passphrase: str,
) -> None:
    member_meta = {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "encrypted": bool(passphrase),
    }
    if passphrase:
        encrypted = _encrypt_bytes(payload, passphrase=passphrase)
        member_meta["encryption"] = {key: value for key, value in encrypted.items() if key != "ciphertext_b64"}
        _archive_writestr(archive, member_name, _canonical_json(encrypted))
    else:
        _archive_writestr(archive, member_name, payload)
    manifest.setdefault("members", {})[member_name] = member_meta


def _coerce_embedding_b64(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)


def _normalize_transcript(record: dict[str, Any], *, redact_patterns: list[str]) -> dict[str, Any]:
    normalized = deepcopy(record)
    normalized["transcript_text"] = _redact_text(str(record.get("transcript_text", "")), redact_patterns)
    normalized["embedding_b64"] = _coerce_embedding_b64(record.get("embedding_b64") or record.get("embedding"))
    normalized["tags"] = sorted(str(tag) for tag in normalized.get("tags", []) if str(tag).strip())
    normalized.pop("embedding", None)
    return normalized


def _normalize_node(record: dict[str, Any], *, redact_patterns: list[str], include_embeddings: bool) -> dict[str, Any]:
    normalized = deepcopy(record)
    normalized["source_prompt"] = _redact_text(str(record.get("source_prompt", "")), redact_patterns)
    normalized["tags"] = sorted(str(tag) for tag in normalized.get("tags", []) if str(tag).strip())
    normalized["evidence_records"] = _sorted_records(
        list(normalized.get("evidence_records", [])), "evidence_id", "session_id", "turn_index"
    )
    embedding = normalized.pop("embedding", None)
    if include_embeddings:
        normalized["embedding_b64"] = _coerce_embedding_b64(normalized.get("embedding_b64") or embedding)
    else:
        normalized["embedding_b64"] = ""
    return normalized


def _normalize_edge(record: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(record)
    normalized["shared_entities"] = sorted(
        str(item) for item in normalized.get("shared_entities", []) if str(item).strip()
    )
    return normalized


def _normalize_window(record: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(record)


def _assert_supported_schema_version(schema_version: str) -> None:
    major_text = str(schema_version).split(".", 1)[0].strip() or "0"
    try:
        major = int(major_text)
    except ValueError as exc:
        raise ValidationFailure(f"Invalid schema version '{schema_version}'.") from exc
    if major != ABHI_MAJOR_VERSION:
        raise ValidationFailure(
            f"Unsupported .abhi schema version '{schema_version}'. "
            f"Readers support major version {ABHI_MAJOR_VERSION} only."
        )


def filter_snapshot_by_scope(
    snapshot: dict[str, Any],
    *,
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    if not any((project.strip(), agent_id.strip(), session_id.strip())):
        return deepcopy(snapshot)

    filtered = deepcopy(snapshot)
    filtered["nodes"] = [
        node
        for node in snapshot.get("nodes", [])
        if _scope_match(node, project=project, agent_id=agent_id, session_id=session_id)
    ]
    selected_node_ids = {str(node.get("id", "")).strip() for node in filtered["nodes"]}
    filtered["edges"] = [
        edge
        for edge in snapshot.get("edges", [])
        if str(edge.get("source_id", "")).strip() in selected_node_ids
        and str(edge.get("target_id", "")).strip() in selected_node_ids
    ]
    filtered["transcripts"] = [
        transcript
        for transcript in snapshot.get("transcripts", [])
        if _scope_match(transcript, project=project, agent_id=agent_id, session_id=session_id)
    ]
    selected_window_ids = {
        str(node.get("context_window_id", "")).strip()
        for node in filtered["nodes"]
        if str(node.get("context_window_id", "")).strip()
    }
    filtered["context_windows"] = [
        window
        for window in snapshot.get("context_windows", [])
        if str(window.get("id", "")).strip() in selected_window_ids
    ]
    filtered["context_window_edges"] = [
        edge
        for edge in snapshot.get("context_window_edges", [])
        if str(edge.get("source_window_id", "")).strip() in selected_window_ids
        and str(edge.get("target_window_id", "")).strip() in selected_window_ids
    ]
    filtered["repos"] = deepcopy(snapshot.get("repos", []))
    return filtered


def _scope_filter(
    snapshot: dict[str, Any], *, scope: str, project: str, agent_id: str, session_id: str, since_date: str
) -> dict[str, Any]:
    normalized = scope.strip().lower() or "all"
    filtered = deepcopy(snapshot)
    if normalized in {"project", "session"}:
        filtered = filter_snapshot_by_scope(
            filtered,
            project=project,
            agent_id=agent_id,
            session_id=session_id if normalized == "session" else "",
        )
    elif normalized == "since-date":
        cutoff = since_date.strip()
        if not cutoff:
            raise ValidationFailure("--scope since-date requires --since-date.")
        filtered["nodes"] = [
            node
            for node in filtered.get("nodes", [])
            if str(node.get("updated_at") or node.get("created_at") or "") >= cutoff
        ]
        node_ids = {str(node.get("id", "")).strip() for node in filtered["nodes"]}
        filtered["edges"] = [
            edge
            for edge in filtered.get("edges", [])
            if str(edge.get("source_id", "")).strip() in node_ids and str(edge.get("target_id", "")).strip() in node_ids
        ]
        filtered["transcripts"] = [
            row for row in filtered.get("transcripts", []) if str(row.get("observed_at", "")).strip() >= cutoff
        ]
    elif normalized != "all":
        raise ValidationFailure("scope must be one of: all, project, session, since-date.")
    return filtered


def build_abhi_document(
    snapshot: dict[str, Any],
    *,
    scope: str = "all",
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
    since_date: str = "",
    include_embeddings: bool = True,
    redact_patterns: list[str] | None = None,
    encrypted: bool = False,
    include_low_confidence_edges: bool = False,
    low_confidence_threshold: float = 0.7,
    strict_export: bool = False,
    include_deps: bool = False,
) -> dict[str, Any]:
    redact_patterns = redact_patterns or []
    filtered = _scope_filter(
        snapshot,
        scope=scope,
        project=project,
        agent_id=agent_id,
        session_id=session_id,
        since_date=since_date,
    )
    transcripts = _sorted_records(
        [_normalize_transcript(item, redact_patterns=redact_patterns) for item in filtered.get("transcripts", [])],
        "id",
        "turn_pair_id",
        "observed_at",
    )
    nodes = _sorted_records(
        [
            _normalize_node(item, redact_patterns=redact_patterns, include_embeddings=include_embeddings)
            for item in filtered.get("nodes", [])
        ],
        "id",
        "source_turn_pair_id",
        "updated_at",
    )
    all_edges = _sorted_records(
        [_normalize_edge(item) for item in filtered.get("edges", [])], "id", "source_id", "target_id", "relationship"
    )

    # Filter low-confidence RELATES_TO edges unless caller opts in.
    edges_filtered_count = 0
    if not include_low_confidence_edges:
        kept: list[dict[str, Any]] = []
        for edge in all_edges:
            if str(edge.get("relationship", "")).lower() == "relates_to":
                meta = edge.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        import json as _json

                        meta = _json.loads(meta)
                        if not isinstance(meta, dict):
                            logger.warning(
                                "Edge %s has non-dict metadata, defaulting to {}: got %s",
                                edge.get("id", "<unknown>"),
                                type(meta).__name__,
                            )
                            meta = {}
                    except _json.JSONDecodeError as exc:
                        logger.warning(
                            "Edge %s has malformed metadata, defaulting to {}: %s",
                            edge.get("id", "<unknown>"),
                            exc,
                        )
                        meta = {}
                confidence = float(meta.get("edge_confidence", 1.0))
                if confidence < low_confidence_threshold:
                    edges_filtered_count += 1
                    continue
            kept.append(edge)
        edges = kept
    else:
        edges = all_edges

    context_windows = _sorted_records(
        [_normalize_window(item) for item in filtered.get("context_windows", [])], "id", "session_id"
    )
    repos = _sorted_records(list(filtered.get("repos", [])), "id", "name")
    context_window_edges = _sorted_records(
        list(filtered.get("context_window_edges", [])), "id", "source_window_id", "target_window_id", "edge_type"
    )
    manifest = {
        "schema_version": ABHI_SPEC_VERSION,
        "tenant": str(filtered.get("tenant_id", "")),
        "agent_id": agent_id or str(filtered.get("agent_id", "")),
        "project": project or str(filtered.get("project", "")),
        "session_id": session_id or str(filtered.get("session_id", "")),
        "embedding_model_id": str(filtered.get("embedding_model_id") or _infer_embedding_model_id(nodes, transcripts)),
        "embedding_dim": int(filtered.get("embedding_dim") or _infer_embedding_dim(nodes, transcripts)),
        "encryption": {
            "enabled": encrypted,
            "algorithm": ABHI_ENCRYPTION_ALGORITHM if encrypted else "",
        },
        "signatures": {
            "algorithm": ABHI_SIGNATURE_ALGORITHM,
            "present": False,
        },
        "scope": scope,
        "includes_embeddings": include_embeddings,
        "export_context": {
            "edge_filter": {
                "include_low_confidence_edges": include_low_confidence_edges,
                "low_confidence_threshold": low_confidence_threshold,
                "edges_total": len(all_edges),
                "edges_exported": len(edges),
                "edges_filtered": edges_filtered_count,
            }
        },
        "counts": {
            "transcripts": len(transcripts),
            "nodes": len(nodes),
            "edges": len(edges),
            "context_windows": len(context_windows),
        },
        "members": {},
        "ui": deepcopy(filtered.get("ui", {})),
        "repos": repos,
        "context_window_edges": context_window_edges,
    }
    document = {
        "manifest": manifest,
        "transcripts": transcripts,
        "nodes": nodes,
        "edges": edges,
        "context_windows": context_windows,
    }
    manifest["content_hash"] = _hash_with_prefix(compute_abhi_hash(document))
    dangling_export = _find_dangling_edges(document)
    if dangling_export:
        if strict_export:
            raise DanglingEdgeError(
                f"Export contains {len(dangling_export)} dangling edge(s). "
                "Use --include-deps or remove --strict-export."
            )
        if include_deps:
            logger.info("Walking dangling edge targets to include referenced nodes (not yet implemented, continuing)")
        else:
            logger.warning(
                "Export contains %d dangling edge(s): %s",
                len(dangling_export),
                ", ".join(dangling_export[:5]) + ("..." if len(dangling_export) > 5 else ""),
            )
    return _with_compat_views(document)


def _infer_embedding_model_id(nodes: list[dict[str, Any]], transcripts: list[dict[str, Any]]) -> str:
    for record in [*nodes, *transcripts]:
        value = str(record.get("embedding_model_id", "")).strip()
        if value:
            return value
    return ""


def _infer_embedding_dim(nodes: list[dict[str, Any]], transcripts: list[dict[str, Any]]) -> int:
    for record in [*nodes, *transcripts]:
        value = int(record.get("embedding_dim", 0) or 0)
        if value:
            return value
    return 0


def _signature_payload(document: dict[str, Any]) -> bytes:
    content_hash = _hash_with_prefix(
        str(document.get("manifest", {}).get("content_hash", "")) or compute_abhi_hash(document)
    )
    return content_hash.encode("utf-8")


def _load_or_create_signing_key(signing_key_dir: str | Path) -> tuple[ed25519.Ed25519PrivateKey, Path]:
    key_dir = Path(signing_key_dir).expanduser()
    key_dir.mkdir(parents=True, exist_ok=True)
    private_path = key_dir / "abhi-signing-key.pem"
    if private_path.exists():
        private_key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise ValidationFailure(f"Unsupported signing key in {private_path}.")
        return private_key, private_path
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return private_key, private_path


def write_abhi_document(
    snapshot: dict[str, Any],
    *,
    output_path: str | Path,
    passphrase: str = "",
    scope: str = "all",
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
    since_date: str = "",
    include_embeddings: bool = True,
    redact_patterns: list[str] | None = None,
    sign: bool = False,
    signing_key_dir: str | Path | None = None,
    include_low_confidence_edges: bool = False,
    low_confidence_threshold: float = 0.7,
) -> AbhiExportResult:
    destination = Path(output_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = build_abhi_document(
        snapshot,
        scope=scope,
        project=project,
        agent_id=agent_id,
        session_id=session_id,
        since_date=since_date,
        include_embeddings=include_embeddings,
        redact_patterns=redact_patterns,
        encrypted=bool(passphrase),
        include_low_confidence_edges=include_low_confidence_edges,
        low_confidence_threshold=low_confidence_threshold,
    )
    manifest = document["manifest"]
    # Write the ZIP to a temp file first, then stream magic + ZIP to the final
    # destination.  This avoids holding the entire archive in memory, which
    # matters for large graphs (hundreds of MB of embeddings).
    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".abhi.tmp", dir=destination.parent)
    tmp_path = Path(tmp_path_str)
    try:
        os.close(tmp_fd)
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _write_member(
                archive,
                manifest,
                ABHI_TRANSCRIPTS_MEMBER,
                _record_lines(document["transcripts"]),
                passphrase=passphrase,
            )
            _write_member(archive, manifest, ABHI_NODES_MEMBER, _record_lines(document["nodes"]), passphrase=passphrase)
            _write_member(archive, manifest, ABHI_EDGES_MEMBER, _record_lines(document["edges"]), passphrase=passphrase)
            _write_member(
                archive,
                manifest,
                ABHI_CONTEXT_WINDOWS_MEMBER,
                _record_lines(document["context_windows"]),
                passphrase=passphrase,
            )
            if sign:
                private_key, _ = _load_or_create_signing_key(signing_key_dir or "~/.waggle/keys")
                signature = private_key.sign(_signature_payload(document))
                public_key = private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                _archive_writestr(archive, ABHI_SIGNATURE_MEMBER, signature)
                _archive_writestr(archive, ABHI_PUBLIC_KEY_MEMBER, public_key)
                manifest["signatures"] = {
                    "algorithm": ABHI_SIGNATURE_ALGORITHM,
                    "present": True,
                }
            manifest["content_hash"] = _hash_with_prefix(compute_abhi_hash(document))
            _archive_writestr(archive, ABHI_MANIFEST_MEMBER, _canonical_json(manifest))
        # Stream magic bytes + ZIP content to the final destination.
        with destination.open("wb") as out_fh:
            out_fh.write(ABHI_MAGIC)
            with tmp_path.open("rb") as zip_fh:
                shutil.copyfileobj(zip_fh, out_fh)
    finally:
        tmp_path.unlink(missing_ok=True)
    return AbhiExportResult(
        output_path=str(destination),
        tenant_id=str(manifest.get("tenant", "")),
        schema_version=ABHI_MAJOR_VERSION,
        abhi_spec_version=ABHI_SPEC_VERSION,
        node_count=len(document["nodes"]),
        edge_count=len(document["edges"]),
        content_hash=str(manifest.get("content_hash", "")),
        embedding_count=sum(1 for node in document["nodes"] if str(node.get("embedding_b64", "")).strip()),
        encrypted=bool(passphrase),
        encryption_algorithm=ABHI_ENCRYPTION_ALGORITHM if passphrase else "",
        executed_actions=[],
        export_context=dict(manifest.get("export_context", {})),
    )


def load_abhi_document(input_path: str | Path, passphrase: str = "") -> dict[str, Any]:
    source = Path(input_path).expanduser()
    raw = source.read_bytes()

    # --- Format detection ---
    # Guard against truncated / empty files before slicing.
    if len(raw) < 4:
        raise ValidationFailure(f"{source} is not a valid .abhi file (file is too short or empty).")

    header = raw[:ABHI_MAGIC_LEN]
    if header == ABHI_MAGIC:
        # v1: Waggle magic prefix present — strip it and open the ZIP payload.
        zip_source: str | Path | io.BytesIO = io.BytesIO(raw[ABHI_MAGIC_LEN:])
    elif raw[:4] == _ABHI_ZIP_MAGIC:
        # v0: bare ZIP written before magic bytes were introduced.
        # Warn so users know to re-export with a current Waggle client.
        logger.warning(
            "%s is a legacy v0 .abhi file (no magic bytes). Re-export with Waggle 0.1.15+ to upgrade to the v1 format.",
            source,
        )
        zip_source = source
    else:
        raise ValidationFailure(
            f"{source} is not a valid .abhi file. The file may be corrupt or was not exported by a Waggle MCP client."
        )

    with zipfile.ZipFile(zip_source, "r") as archive:
        if ABHI_MANIFEST_MEMBER not in archive.namelist():
            raise ValidationFailure(f"{source} is missing {ABHI_MANIFEST_MEMBER}.")
        manifest = json.loads(archive.read(ABHI_MANIFEST_MEMBER).decode("utf-8"))
        _assert_supported_schema_version(str(manifest.get("schema_version", "")))
        document = {
            "manifest": manifest,
            "transcripts": _parse_lines(
                _read_member(archive, manifest, ABHI_TRANSCRIPTS_MEMBER, passphrase=passphrase)
            ),
            "nodes": _parse_lines(_read_member(archive, manifest, ABHI_NODES_MEMBER, passphrase=passphrase)),
            "edges": _parse_lines(_read_member(archive, manifest, ABHI_EDGES_MEMBER, passphrase=passphrase)),
            "context_windows": _parse_lines(
                _read_member(archive, manifest, ABHI_CONTEXT_WINDOWS_MEMBER, passphrase=passphrase)
            ),
        }
        if manifest.get("signatures", {}).get("present"):
            document["signature"] = archive.read(ABHI_SIGNATURE_MEMBER)
            document["public_key_pem"] = archive.read(ABHI_PUBLIC_KEY_MEMBER)
        return _with_compat_views(document)


def inspect_abhi_document(document: dict[str, Any], *, input_path: str | Path) -> AbhiInspectResult:
    manifest = document.get("manifest", {})
    node_types = sorted(
        {
            str(node.get("node_type", "")).strip()
            for node in document.get("nodes", [])
            if str(node.get("node_type", "")).strip()
        }
    )
    edge_types = sorted(
        {
            str(edge.get("relationship", "")).strip()
            for edge in document.get("edges", [])
            if str(edge.get("relationship", "")).strip()
        }
    )
    return AbhiInspectResult(
        input_path=str(Path(input_path).expanduser()),
        tenant_id=str(manifest.get("tenant", "")),
        schema_version=ABHI_MAJOR_VERSION,
        abhi_spec_version=str(manifest.get("schema_version", "")) or ABHI_SPEC_VERSION,
        node_count=len(document.get("nodes", [])),
        edge_count=len(document.get("edges", [])),
        node_types=node_types,
        edge_types=edge_types,
        constraint_count=0,
        version_count=1,
        query_count=0,
        event_count=0,
        chunk_count=max(1, (len(document.get("nodes", [])) + ABHI_CHUNK_NODE_LIMIT - 1) // ABHI_CHUNK_NODE_LIMIT),
        load_strategy="chunked" if len(document.get("nodes", [])) > ABHI_CHUNK_NODE_LIMIT else "full",
        preload_chunks=["chunk-0"] if document.get("nodes") else [],
        content_hash=str(manifest.get("content_hash", "")),
        embedding_count=sum(1 for node in document.get("nodes", []) if str(node.get("embedding_b64", "")).strip()),
        encrypted=bool(manifest.get("encryption", {}).get("enabled")),
        encryption_algorithm=str(manifest.get("encryption", {}).get("algorithm", "")),
    )


def _diff_dicts(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _canonical_json(left) != _canonical_json(right)


def _classify_node_identity(node_a, node_b) -> Literal["identical", "modified", "separate"]:
    if node_a["id"] != node_b["id"]:
        return "separate"
    for field in DIFFED_FIELDS:
        if field == "metadata":
            meta_a = node_a.get("metadata") or {}
            meta_b = node_b.get("metadata") or {}
            all_keys = set(meta_a) | set(meta_b)
            for key in all_keys:
                if json.dumps(meta_a.get(key), sort_keys=True, default=str) != json.dumps(
                    meta_b.get(key), sort_keys=True, default=str
                ):
                    return "modified"
        else:
            if json.dumps(node_a.get(field), sort_keys=True, default=str) != json.dumps(
                node_b.get(field), sort_keys=True, default=str
            ):
                return "modified"
    return "identical"


def _classify_edge_identity(edge_a, edge_b) -> Literal["identical", "modified", "separate"]:
    if edge_a["id"] != edge_b["id"]:
        return "separate"
    for field in EDGE_DIFFED_FIELDS:
        if json.dumps(edge_a.get(field), sort_keys=True, default=str) != json.dumps(
            edge_b.get(field), sort_keys=True, default=str
        ):
            return "modified"
    return "identical"


def _compute_node_delta(node_a, node_b) -> list[FieldDelta]:
    deltas = []
    for field in DIFFED_FIELDS:
        if field == "metadata":
            meta_a = (node_a.get("metadata") or {}) if node_a is not None else {}
            meta_b = (node_b.get("metadata") or {}) if node_b is not None else {}
            for key in sorted(set(meta_a) | set(meta_b)):
                old_val = meta_a.get(key)
                new_val = meta_b.get(key)
                if json.dumps(old_val, sort_keys=True, default=str) != json.dumps(new_val, sort_keys=True, default=str):
                    deltas.append(
                        FieldDelta(
                            field=f"metadata.{key}",
                            old_value=old_val if node_a is not None else None,
                            new_value=new_val if node_b is not None else None,
                        )
                    )
        else:
            old_val = node_a.get(field) if node_a is not None else None
            new_val = node_b.get(field) if node_b is not None else None
            if json.dumps(old_val, sort_keys=True, default=str) != json.dumps(new_val, sort_keys=True, default=str):
                deltas.append(FieldDelta(field=field, old_value=old_val, new_value=new_val))
    return deltas


def _compute_edge_delta(edge_a, edge_b) -> list[FieldDelta]:
    deltas = []
    for field in EDGE_DIFFED_FIELDS:
        old_val = edge_a.get(field) if edge_a is not None else None
        new_val = edge_b.get(field) if edge_b is not None else None
        if json.dumps(old_val, sort_keys=True, default=str) != json.dumps(new_val, sort_keys=True, default=str):
            deltas.append(FieldDelta(field=field, old_value=old_val, new_value=new_val))
    return deltas


def _check_schema_version_compatibility(version_a: str, version_b: str, operation: str) -> None:
    def _major(v):
        try:
            return int(str(v).split(".", 1)[0].strip() or "0")
        except ValueError:
            return 0

    if _major(version_a) != _major(version_b):
        raise SchemaVersionError(
            f"Cannot {operation} documents with incompatible schema versions: "
            f"{version_a} vs {version_b}. "
            f"Run 'waggle upgrade <file> --to {version_b}' to upgrade the older document first."
        )


def diff_abhi_documents(
    document_a: dict[str, Any],
    document_b: dict[str, Any],
    *,
    input_path_a: str | Path,
    input_path_b: str | Path,
    base_document: dict[str, Any] | None = None,
) -> FieldLevelDiffResult:
    version_a = str(document_a.get("manifest", {}).get("schema_version", ABHI_SPEC_VERSION))
    version_b = str(document_b.get("manifest", {}).get("schema_version", ABHI_SPEC_VERSION))

    result_warnings: list[str] = []
    schema_version_mismatch = False

    def _major(v: str) -> int:
        try:
            return int(str(v).split(".", 1)[0].strip() or "0")
        except ValueError:
            return 0

    if version_a != version_b:
        if _major(version_a) != _major(version_b):
            raise SchemaVersionError(
                f"Cannot diff documents with incompatible schema versions: "
                f"{version_a} vs {version_b}. "
                f"Run 'waggle upgrade <file> --to {version_b}' to upgrade the older document first."
            )
        else:
            schema_version_mismatch = True
            result_warnings.append(f"Schema version mismatch: {version_a} vs {version_b}. Diff may be incomplete.")

    nodes_a = {str(node.get("id", "")): node for node in document_a.get("nodes", [])}
    nodes_b = {str(node.get("id", "")): node for node in document_b.get("nodes", [])}
    edges_a = {str(edge.get("id", "")): edge for edge in document_a.get("edges", [])}
    edges_b = {str(edge.get("id", "")): edge for edge in document_b.get("edges", [])}

    nodes_added: list[str] = []
    nodes_removed: list[str] = []
    nodes_updated: list[str] = []
    node_records: list[NodeDiffRecord] = []

    all_node_ids = sorted(set(nodes_a) | set(nodes_b))
    for nid in all_node_ids:
        if not nid:
            continue
        na = nodes_a.get(nid)
        nb = nodes_b.get(nid)
        if na is None:
            nodes_added.append(nid)
            deltas = _compute_node_delta(None, nb)
            node_records.append(
                NodeDiffRecord(
                    node_id=nid,
                    classification="added",
                    label=str(nb.get("label", "") or nb.get("content", "")[:60]),
                    deltas=deltas,
                )
            )
        elif nb is None:
            nodes_removed.append(nid)
            deltas = _compute_node_delta(na, None)
            node_records.append(
                NodeDiffRecord(
                    node_id=nid,
                    classification="removed",
                    label=str(na.get("label", "") or na.get("content", "")[:60]),
                    deltas=deltas,
                )
            )
        else:
            classification = _classify_node_identity(na, nb)
            if classification == "modified":
                nodes_updated.append(nid)
                deltas = _compute_node_delta(na, nb)
                node_records.append(
                    NodeDiffRecord(
                        node_id=nid,
                        classification="modified",
                        label=str(nb.get("label", "") or nb.get("content", "")[:60]),
                        deltas=deltas,
                    )
                )
            else:
                node_records.append(
                    NodeDiffRecord(
                        node_id=nid,
                        classification="identical",
                        label=str(nb.get("label", "") or nb.get("content", "")[:60]),
                        deltas=[],
                    )
                )

    edges_added: list[str] = []
    edges_removed: list[str] = []
    edges_updated: list[str] = []
    edge_records: list[EdgeDiffRecord] = []

    all_edge_ids = sorted(set(edges_a) | set(edges_b))
    for eid in all_edge_ids:
        if not eid:
            continue
        ea = edges_a.get(eid)
        eb = edges_b.get(eid)
        if ea is None:
            edges_added.append(eid)
            deltas = _compute_edge_delta(None, eb)
            edge_records.append(EdgeDiffRecord(edge_id=eid, classification="added", deltas=deltas))
        elif eb is None:
            edges_removed.append(eid)
            deltas = _compute_edge_delta(ea, None)
            edge_records.append(EdgeDiffRecord(edge_id=eid, classification="removed", deltas=deltas))
        else:
            classification = _classify_edge_identity(ea, eb)
            if classification == "modified":
                edges_updated.append(eid)
                deltas = _compute_edge_delta(ea, eb)
                edge_records.append(EdgeDiffRecord(edge_id=eid, classification="modified", deltas=deltas))
            else:
                edge_records.append(EdgeDiffRecord(edge_id=eid, classification="identical", deltas=[]))

    # Three-way conflict detection
    conflict_records: list[MergeConflictRecord] = []
    diff_mode: Literal["two_way", "three_way"] = "two_way"
    input_path_base_str = ""
    if base_document is not None:
        diff_mode = "three_way"
        input_path_base_str = ""  # not a file path in this context
        nodes_base = {str(n.get("id", "")): n for n in base_document.get("nodes", [])}
        for nid in sorted(set(nodes_a) & set(nodes_b)):
            if not nid:
                continue
            base_node = nodes_base.get(nid)
            left_node = nodes_a.get(nid)
            right_node = nodes_b.get(nid)
            if base_node is None or left_node is None or right_node is None:
                continue
            for field in DIFFED_FIELDS:
                base_val = base_node.get(field)
                left_val = left_node.get(field)
                right_val = right_node.get(field)
                left_changed = json.dumps(base_val, sort_keys=True, default=str) != json.dumps(
                    left_val, sort_keys=True, default=str
                )
                right_changed = json.dumps(base_val, sort_keys=True, default=str) != json.dumps(
                    right_val, sort_keys=True, default=str
                )
                if (
                    left_changed
                    and right_changed
                    and json.dumps(left_val, sort_keys=True, default=str)
                    != json.dumps(right_val, sort_keys=True, default=str)
                ):
                    conflict_records.append(
                        MergeConflictRecord(
                            object_id=nid,
                            object_type="node",
                            field=field,
                            base_value=base_val,
                            left_value=left_val,
                            right_value=right_val,
                        )
                    )

        # Edge three-way conflict detection
        edges_base = {str(e.get("id", "")): e for e in base_document.get("edges", [])}
        for eid in sorted(set(edges_a) & set(edges_b)):
            if not eid:
                continue
            base_edge = edges_base.get(eid)
            left_edge = edges_a.get(eid)
            right_edge = edges_b.get(eid)
            if base_edge is None or left_edge is None or right_edge is None:
                continue
            for field in EDGE_DIFFED_FIELDS:
                base_val = base_edge.get(field)
                left_val = left_edge.get(field)
                right_val = right_edge.get(field)
                left_changed = json.dumps(base_val, sort_keys=True, default=str) != json.dumps(
                    left_val, sort_keys=True, default=str
                )
                right_changed = json.dumps(base_val, sort_keys=True, default=str) != json.dumps(
                    right_val, sort_keys=True, default=str
                )
                if (
                    left_changed
                    and right_changed
                    and json.dumps(left_val, sort_keys=True, default=str)
                    != json.dumps(right_val, sort_keys=True, default=str)
                ):
                    conflict_records.append(
                        MergeConflictRecord(
                            object_id=eid,
                            object_type="edge",
                            field=field,
                            base_value=base_val,
                            left_value=left_val,
                            right_value=right_val,
                        )
                    )

    return FieldLevelDiffResult(
        input_path_a=str(Path(input_path_a).expanduser()),
        input_path_b=str(Path(input_path_b).expanduser()),
        input_path_base=input_path_base_str,
        abhi_spec_version_a=version_a,
        abhi_spec_version_b=version_b,
        diff_mode=diff_mode,
        nodes_added=nodes_added,
        nodes_removed=nodes_removed,
        nodes_updated=nodes_updated,
        edges_added=edges_added,
        edges_removed=edges_removed,
        edges_updated=edges_updated,
        node_records=node_records,
        edge_records=edge_records,
        conflict_records=conflict_records,
        warnings=result_warnings,
        schema_version_mismatch=schema_version_mismatch,
    )


def _set_field_val(item: dict[str, Any], field: str, value: Any) -> None:
    """Set a field on an item, supporting metadata.* nested fields."""
    if field.startswith("metadata."):
        key = field[len("metadata.") :]
        if "metadata" not in item or item["metadata"] is None:
            item["metadata"] = {}
        item["metadata"][key] = value
    else:
        item[field] = value


def _apply_merge_strategy(
    item_id: str,
    object_type: Literal["node", "edge"],
    base_item: dict[str, Any] | None,
    left_item: dict[str, Any],
    right_item: dict[str, Any],
    *,
    strategy: str,
    strategy_config: MergeStrategyConfig | None,
    conflict_records: list[MergeConflictRecord],
    contradict_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply the merge strategy to a single conflicting item."""
    from uuid import uuid4

    fields = DIFFED_FIELDS if object_type == "node" else EDGE_DIFFED_FIELDS
    result = deepcopy(right_item)  # start with right as base

    for field in fields:
        base_val = base_item.get(field) if base_item is not None else None
        left_val = left_item.get(field)
        right_val = right_item.get(field)

        left_changed = json.dumps(base_val, sort_keys=True, default=str) != json.dumps(
            left_val, sort_keys=True, default=str
        )
        right_changed = json.dumps(base_val, sort_keys=True, default=str) != json.dumps(
            right_val, sort_keys=True, default=str
        )

        if not left_changed and not right_changed:
            continue
        if left_changed and not right_changed:
            _set_field_val(result, field, left_val)
            continue
        if not left_changed and right_changed:
            _set_field_val(result, field, right_val)
            continue

        # Both changed — conflict
        # Determine effective strategy for this field.
        # Apply type_overrides first (broad), then field_overrides (specific)
        # so that field-level overrides take precedence over type-level ones.
        effective_strategy = strategy
        if strategy_config is not None:
            if object_type == "node":
                node_type_val = str(left_item.get("node_type", ""))
                for override in strategy_config.type_overrides:
                    if override.node_type == node_type_val:
                        effective_strategy = override.strategy
                        break
            for override in strategy_config.field_overrides:
                if override.field == field:
                    effective_strategy = override.strategy
                    break

        resolved_by = effective_strategy
        if effective_strategy == "prefer_left":
            resolved_val = left_val
        elif effective_strategy == "prefer_right":
            resolved_val = right_val
        elif effective_strategy == "last_write_wins":
            left_ts = str(left_item.get("updated_at") or left_item.get("created_at") or "")
            right_ts = str(right_item.get("updated_at") or right_item.get("created_at") or "")
            if right_ts >= left_ts:
                resolved_val = right_val
                resolved_by = "last_write_wins"
            else:
                resolved_val = left_val
                resolved_by = "last_write_wins"
        elif effective_strategy == "contradict":
            resolved_val = right_val
            resolved_by = "contradict"
            # Schedule a CONTRADICTS edge.
            # For node conflicts, link the node to itself (self-loop).
            # For edge conflicts, link the edge's source and target nodes
            # to avoid creating a dangling edge that references an edge ID.
            if object_type == "edge":
                contradict_source = str(left_item.get("source_id", item_id))
                contradict_target = str(left_item.get("target_id", item_id))
            else:
                contradict_source = item_id
                contradict_target = item_id
            contradict_edges.append(
                {
                    "id": str(uuid4()),
                    "source_id": contradict_source,
                    "target_id": contradict_target,
                    "relationship": "contradicts",
                    "weight": 1.0,
                    "metadata": {
                        "conflict_field": field,
                        "conflict_edge_id": item_id if object_type == "edge" else "",
                        "left_value": left_val,
                        "right_value": right_val,
                        "auto_generated": True,
                    },
                }
            )
        else:
            resolved_val = right_val
            resolved_by = effective_strategy

        _set_field_val(result, field, resolved_val)
        conflict_records.append(
            MergeConflictRecord(
                object_id=item_id,
                object_type=object_type,
                field=field,
                base_value=base_val,
                left_value=left_val,
                right_value=right_val,
                resolved_by=resolved_by,
                resolved_value=resolved_val,
            )
        )

    return result


def _merge_records(
    base_items: list[dict[str, Any]],
    left_items: list[dict[str, Any]],
    right_items: list[dict[str, Any]],
    *,
    object_type: Literal["node", "edge"] = "node",
    merge_strategy: str = "contradict",
    strategy_config: MergeStrategyConfig | None = None,
    conflict_records: list[MergeConflictRecord],
    contradict_edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    base_map = {str(item.get("id", "")): item for item in base_items if str(item.get("id", "")).strip()}
    left_map = {str(item.get("id", "")): item for item in left_items if str(item.get("id", "")).strip()}
    right_map = {str(item.get("id", "")): item for item in right_items if str(item.get("id", "")).strip()}
    merged: dict[str, dict[str, Any]] = {}
    for item_id in sorted(set(base_map) | set(left_map) | set(right_map)):
        left_item = left_map.get(item_id)
        right_item = right_map.get(item_id)
        if left_item is None and right_item is None:
            continue
        if left_item is None:
            merged[item_id] = deepcopy(right_item)
            continue
        if right_item is None:
            merged[item_id] = deepcopy(left_item)
            continue
        if not _diff_dicts(left_item, right_item):
            merged[item_id] = deepcopy(left_item)
            continue
        base_item = base_map.get(item_id)
        # Check if both sides changed relative to base
        left_changed_from_base = base_item is None or _diff_dicts(base_item, left_item)
        right_changed_from_base = base_item is None or _diff_dicts(base_item, right_item)
        if left_changed_from_base and right_changed_from_base:
            merged[item_id] = _apply_merge_strategy(
                item_id,
                object_type,
                base_item,
                left_item,
                right_item,
                strategy=merge_strategy,
                strategy_config=strategy_config,
                conflict_records=conflict_records,
                contradict_edges=contradict_edges,
            )
        elif left_changed_from_base:
            merged[item_id] = deepcopy(left_item)
        else:
            merged[item_id] = deepcopy(right_item)
    return list(merged.values())


def merge_abhi_documents(
    base_document: dict[str, Any],
    left_document: dict[str, Any],
    right_document: dict[str, Any],
    *,
    base_input_path: str | Path,
    left_input_path: str | Path,
    right_input_path: str | Path,
    output_path: str | Path,
    merge_strategy: str = "contradict",
    strategy_config: MergeStrategyConfig | None = None,
    passphrase: str = "",
    dry_run: bool = False,
) -> AbhiMergeResult:
    """Merge left and right .abhi documents against a common base.

    Supported strategies:

    - ``contradict``: retain the right value and create a CONTRADICTS edge.
    - ``last_write_wins``: retain the value with the newest timestamp.
    - ``prefer_left``: retain the left/local value during a conflict.
    - ``prefer_right``: retain the right/remote value during a conflict.
    """
    merge_strategy = _validate_abhi_merge_strategy(merge_strategy)
    if strategy_config is not None:
        for override in strategy_config.type_overrides:
            _validate_abhi_merge_strategy(override.strategy)
        for override in strategy_config.field_overrides:
            _validate_abhi_merge_strategy(override.strategy)

    import time

    _t0 = time.monotonic()

    conflict_records: list[MergeConflictRecord] = []
    contradict_edges: list[dict[str, Any]] = []

    merged_nodes = _merge_records(
        base_document.get("nodes", []),
        left_document.get("nodes", []),
        right_document.get("nodes", []),
        object_type="node",
        merge_strategy=merge_strategy,
        strategy_config=strategy_config,
        conflict_records=conflict_records,
        contradict_edges=contradict_edges,
    )
    merged_edges = _merge_records(
        base_document.get("edges", []),
        left_document.get("edges", []),
        right_document.get("edges", []),
        object_type="edge",
        merge_strategy=merge_strategy,
        strategy_config=strategy_config,
        conflict_records=conflict_records,
        contradict_edges=contradict_edges,
    )
    # Add contradict edges from node conflicts
    merged_edges.extend(contradict_edges)

    merged_transcripts = _merge_records(
        base_document.get("transcripts", []),
        left_document.get("transcripts", []),
        right_document.get("transcripts", []),
        object_type="node",
        merge_strategy=merge_strategy,
        strategy_config=strategy_config,
        conflict_records=conflict_records,
        contradict_edges=[],
    )
    merged_windows = _merge_records(
        base_document.get("context_windows", []),
        left_document.get("context_windows", []),
        right_document.get("context_windows", []),
        object_type="node",
        merge_strategy=merge_strategy,
        strategy_config=strategy_config,
        conflict_records=conflict_records,
        contradict_edges=[],
    )

    merged_snapshot = {
        "tenant_id": str(
            right_document.get("manifest", {}).get("tenant") or left_document.get("manifest", {}).get("tenant", "")
        ),
        "transcripts": merged_transcripts,
        "nodes": merged_nodes,
        "edges": merged_edges,
        "context_windows": merged_windows,
        "ui": deepcopy(
            right_document.get("manifest", {}).get("ui", {}) or left_document.get("manifest", {}).get("ui", {})
        ),
        "repos": deepcopy(
            right_document.get("manifest", {}).get("repos", []) or left_document.get("manifest", {}).get("repos", [])
        ),
        "context_window_edges": deepcopy(
            right_document.get("manifest", {}).get("context_window_edges", [])
            or left_document.get("manifest", {}).get("context_window_edges", [])
        ),
        "embedding_model_id": str(
            right_document.get("manifest", {}).get("embedding_model_id")
            or left_document.get("manifest", {}).get("embedding_model_id", "")
        ),
        "embedding_dim": int(
            right_document.get("manifest", {}).get("embedding_dim")
            or left_document.get("manifest", {}).get("embedding_dim", 0)
            or 0
        ),
    }

    conflicts_str = [f"Conflict on {r.object_id} field={r.field}" for r in conflict_records]

    if dry_run:
        _elapsed = time.monotonic() - _t0
        logger.info(
            "Dry-run merge completed in %.3fs: %d nodes, %d edges, %d conflicts",
            _elapsed,
            len(merged_nodes),
            len(merged_edges),
            len(conflict_records),
        )
        return AbhiMergeResult(
            base_input_path=str(base_input_path),
            left_input_path=str(left_input_path),
            right_input_path=str(right_input_path),
            output_path="",
            merge_strategy=merge_strategy,
            abhi_spec_version=ABHI_SPEC_VERSION,
            nodes_merged=len(merged_nodes),
            edges_merged=len(merged_edges),
            conflicts=conflicts_str,
            content_hash="",
            embedding_count=0,
            encrypted=False,
            encryption_algorithm="",
            executed_actions=[],
            conflict_records=conflict_records,
            dry_run=True,
            hash_verified=False,
            dangling_edges_dropped=[],
            contradict_edges_added=len(contradict_edges),
        )

    exported = write_abhi_document(merged_snapshot, output_path=output_path, passphrase=passphrase)

    # Hash verification
    hash_verified = False
    try:
        output_doc = load_abhi_document(output_path, passphrase=passphrase)
        actual_hash = compute_abhi_hash(output_doc)
        expected_hash = str(output_doc.get("manifest", {}).get("content_hash", "")).removeprefix("sha256:")
        if actual_hash == expected_hash:
            hash_verified = True
        else:
            Path(output_path).unlink(missing_ok=True)
            raise HashVerificationError(
                f"Post-merge hash verification failed: expected {expected_hash}, got {actual_hash}"
            )
    except HashVerificationError:
        raise
    except Exception as exc:
        logger.warning("Hash verification failed: %s", exc)

    _elapsed = time.monotonic() - _t0
    logger.info(
        "Merge completed in %.3fs: %d nodes, %d edges, %d conflicts",
        _elapsed,
        len(merged_nodes),
        len(merged_edges),
        len(conflict_records),
    )

    return AbhiMergeResult(
        base_input_path=str(base_input_path),
        left_input_path=str(left_input_path),
        right_input_path=str(right_input_path),
        output_path=exported.output_path,
        merge_strategy=merge_strategy,
        abhi_spec_version=ABHI_SPEC_VERSION,
        nodes_merged=len(merged_nodes),
        edges_merged=len(merged_edges),
        conflicts=conflicts_str,
        content_hash=exported.content_hash,
        embedding_count=exported.embedding_count,
        encrypted=exported.encrypted,
        encryption_algorithm=exported.encryption_algorithm,
        executed_actions=[],
        conflict_records=conflict_records,
        dry_run=False,
        hash_verified=hash_verified,
        dangling_edges_dropped=[],
        contradict_edges_added=len(contradict_edges),
    )


def _matches_query(node: dict[str, Any], query_text: str) -> bool:
    lowered = query_text.lower()
    if lowered.startswith("find nodes where"):
        text = lowered
        if "type='" in text:
            expected_type = text.split("type='", 1)[1].split("'", 1)[0]
            if str(node.get("node_type", "")).lower() != expected_type:
                return False
        if "content contains '" in text:
            expected = text.split("content contains '", 1)[1].split("'", 1)[0]
            return expected in str(node.get("content", "")).lower()
        return True
    haystack = " ".join(
        [
            str(node.get("label", "")),
            str(node.get("content", "")),
            " ".join(str(tag) for tag in node.get("tags", [])),
        ]
    ).lower()
    return lowered in haystack


def execute_abhi_query(document: dict[str, Any], *, query_id: str = "", query_text: str = "") -> dict[str, Any]:
    effective_query = query_text.strip() or query_id.strip()
    if not effective_query:
        raise ValidationFailure("Provide query_text or query_id.")
    if query_text.strip():
        matched_nodes = [node for node in document.get("nodes", []) if _matches_query(node, effective_query)]
    else:
        matched_nodes = list(document.get("nodes", []))[:ABHI_CHUNK_NODE_LIMIT]
    node_ids = {str(node.get("id", "")).strip() for node in matched_nodes}
    matched_edges = [
        edge
        for edge in document.get("edges", [])
        if str(edge.get("source_id", "")).strip() in node_ids and str(edge.get("target_id", "")).strip() in node_ids
    ]
    chunk_ids = sorted(
        {
            f"chunk-{index // ABHI_CHUNK_NODE_LIMIT}"
            for index, node in enumerate(document.get("nodes", []))
            if str(node.get("id", "")).strip() in node_ids
        }
    )
    compatible_nodes = []
    for node in matched_nodes:
        enriched = deepcopy(node)
        enriched["type"] = enriched.get("node_type", "")
        compatible_nodes.append(enriched)
    return {
        "query_id": query_id,
        "query": effective_query,
        "nodes": compatible_nodes,
        "edges": matched_edges,
        "chunk_ids": chunk_ids,
    }


def load_abhi_chunks(
    document: dict[str, Any],
    *,
    chunk_ids: list[str] | None = None,
    query_text: str = "",
) -> dict[str, Any]:
    nodes = list(document.get("nodes", []))
    selected_chunk_ids = [chunk_id for chunk_id in (chunk_ids or []) if str(chunk_id).strip()]
    if not selected_chunk_ids and query_text.strip():
        selected_chunk_ids = execute_abhi_query(document, query_text=query_text)["chunk_ids"]
    if not selected_chunk_ids:
        selected_chunk_ids = ["chunk-0"] if nodes else []
    selected_indexes: set[int] = set()
    for chunk_id in selected_chunk_ids:
        if not chunk_id.startswith("chunk-"):
            continue
        try:
            chunk_index = int(chunk_id.split("-", 1)[1])
        except ValueError:
            continue
        start = chunk_index * ABHI_CHUNK_NODE_LIMIT
        end = start + ABHI_CHUNK_NODE_LIMIT
        selected_indexes.update(range(start, min(end, len(nodes))))
    chunk_nodes = [nodes[index] for index in sorted(selected_indexes) if 0 <= index < len(nodes)]
    node_ids = {str(node.get("id", "")).strip() for node in chunk_nodes}
    chunk_edges = [
        edge
        for edge in document.get("edges", [])
        if str(edge.get("source_id", "")).strip() in node_ids and str(edge.get("target_id", "")).strip() in node_ids
    ]
    return {
        "chunk_ids": selected_chunk_ids,
        "nodes": chunk_nodes,
        "edges": chunk_edges,
        "available_chunk_count": max(1, (len(nodes) + ABHI_CHUNK_NODE_LIMIT - 1) // ABHI_CHUNK_NODE_LIMIT)
        if nodes
        else 0,
        "load_strategy": "chunked" if len(nodes) > ABHI_CHUNK_NODE_LIMIT else "full",
        "query": query_text,
    }


def query_abhi_file(
    input_path: str | Path, *, query_id: str = "", query_text: str = "", passphrase: str = ""
) -> AbhiQueryResult:
    source = Path(input_path).expanduser()
    document = load_abhi_document(source, passphrase=passphrase)
    payload = execute_abhi_query(document, query_id=query_id, query_text=query_text)
    return AbhiQueryResult(
        input_path=str(source),
        query_id=query_id,
        name=query_id,
        query=payload["query"],
        summary=f"{len(payload['nodes'])} nodes matched.",
        node_count=len(payload["nodes"]),
        edge_count=len(payload["edges"]),
        node_ids=[str(node.get("id", "")) for node in payload["nodes"]],
        edge_ids=[str(edge.get("id", "")) for edge in payload["edges"]],
        chunk_ids=payload["chunk_ids"],
        scanned_chunk_count=max(
            1, (len(document.get("nodes", [])) + ABHI_CHUNK_NODE_LIMIT - 1) // ABHI_CHUNK_NODE_LIMIT
        )
        if document.get("nodes")
        else 0,
        executed_actions=dispatch_abhi_event(
            document, event_name="on_query", persist=False, input_path=source, query_payload=payload
        ),
    )


def load_abhi_chunk_file(
    input_path: str | Path,
    *,
    chunk_ids: list[str] | None = None,
    query_id: str = "",
    query_text: str = "",
    passphrase: str = "",
) -> AbhiChunkLoadResult:
    source = Path(input_path).expanduser()
    document = load_abhi_document(source, passphrase=passphrase)
    selection_query = query_text.strip() or query_id.strip()
    payload = load_abhi_chunks(document, chunk_ids=chunk_ids or [], query_text=selection_query)
    return AbhiChunkLoadResult(
        input_path=str(source),
        chunk_ids=payload["chunk_ids"],
        load_strategy=payload["load_strategy"],
        node_count=len(payload["nodes"]),
        edge_count=len(payload["edges"]),
        available_chunk_count=payload["available_chunk_count"],
        query=payload["query"],
        node_ids=[str(node.get("id", "")) for node in payload["nodes"]],
        edge_ids=[str(edge.get("id", "")) for edge in payload["edges"]],
    )


def dispatch_abhi_event(
    document: dict[str, Any],
    *,
    event_name: str,
    persist: bool,
    input_path: str | Path | None = None,
    query_payload: dict[str, Any] | None = None,
) -> list[str]:
    action_map = {
        "on_export": ["exported_abhi"],
        "on_import": ["imported_abhi"],
        "on_query": ["queried_abhi"],
        "on_merge": ["merged_abhi"],
    }
    return action_map.get(event_name, [])


def _find_dangling_edges(document: dict[str, Any]) -> list[str]:
    """Return edge IDs whose source_id or target_id is not in the document's node set."""
    node_ids = {str(n["id"]) for n in document.get("nodes", []) if n.get("id")}
    dangling: list[str] = []
    for edge in document.get("edges", []):
        source = str(edge.get("source_id", ""))
        target = str(edge.get("target_id", ""))
        if source not in node_ids or target not in node_ids:
            dangling.append(str(edge.get("id", "")))
    return dangling


def validate_abhi_signature(
    document: dict[str, Any],
    *,
    trusted_public_key_path: str | Path | None = None,
) -> None:
    """Verify the Ed25519 signature on a signed .abhi document.

    If *trusted_public_key_path* is provided, the public key is loaded from
    that external file.  This is the **recommended** mode because it
    prevents an attacker from re-signing a tampered archive with their own
    key pair and bundling it inside the ZIP.

    When *trusted_public_key_path* is ``None`` the function falls back to
    the public key embedded inside the archive and emits a warning.  Use
    this fallback only for quick smoke-checks, never for trust decisions.
    """
    if not document.get("manifest", {}).get("signatures", {}).get("present"):
        raise ValidationFailure("This .abhi file is not signed.")

    if trusted_public_key_path is not None:
        key_path = Path(trusted_public_key_path).expanduser()
        if not key_path.exists():
            raise ValidationFailure(f"Trusted public key file not found: {key_path}")
        public_key = serialization.load_pem_public_key(key_path.read_bytes())
    else:
        warnings.warn(
            "Verifying .abhi signature using the public key bundled inside "
            "the archive.  This does NOT protect against an attacker who "
            "re-signs a tampered file.  Pass trusted_public_key_path for "
            "genuine trust verification.",
            UserWarning,
            stacklevel=2,
        )
        public_key = serialization.load_pem_public_key(document["public_key_pem"])

    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise ValidationFailure("Unsupported ABHI public key.")
    try:
        public_key.verify(document["signature"], _signature_payload(document))
    except InvalidSignature as exc:
        raise ValidationFailure("ABHI signature verification failed.") from exc


def validate_abhi_document(document: dict[str, Any], *, input_path: str | Path) -> AbhiValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    manifest = document.get("manifest", {})
    try:
        _assert_supported_schema_version(str(manifest.get("schema_version", "")))
    except ValidationFailure as exc:
        errors.append(str(exc))
    actual_hash = compute_abhi_hash(document)
    expected_hash = str(manifest.get("content_hash", ""))
    if expected_hash and expected_hash.removeprefix("sha256:") != actual_hash.removeprefix("sha256:"):
        errors.append("Manifest content hash does not match payload.")
    if manifest.get("signatures", {}).get("present"):
        try:
            validate_abhi_signature(document)
        except ValidationFailure as exc:
            errors.append(str(exc))
    if not document.get("nodes") and not document.get("transcripts"):
        warnings.append("ABHI payload is empty.")
    dangling_edges = _find_dangling_edges(document)
    dangling_edge_count = len(dangling_edges)
    boundary_warning = ""
    if dangling_edges:
        boundary_warning = (
            f"Document contains {dangling_edge_count} dangling edge(s): "
            f"{', '.join(dangling_edges[:5])}"
            f"{'...' if dangling_edge_count > 5 else ''}"
        )
        warnings.append(boundary_warning)
    return AbhiValidationResult(
        input_path=str(Path(input_path).expanduser()),
        valid=not errors,
        errors=errors,
        warnings=warnings,
        node_count=len(document.get("nodes", [])),
        edge_count=len(document.get("edges", [])),
        content_hash=_hash_with_prefix(actual_hash.removeprefix("sha256:")),
        abhi_spec_version=str(manifest.get("schema_version", "")) or ABHI_SPEC_VERSION,
        embedding_count=sum(1 for node in document.get("nodes", []) if str(node.get("embedding_b64", "")).strip()),
        encrypted=bool(manifest.get("encryption", {}).get("enabled")),
        encryption_algorithm=str(manifest.get("encryption", {}).get("algorithm", "")),
        dangling_edges=dangling_edges,
        dangling_edge_count=dangling_edge_count,
        boundary_warning=boundary_warning,
    )


def _decode_embedding(embedding_b64: str) -> bytes | None:
    if not embedding_b64.strip():
        return None
    return base64.b64decode(embedding_b64.encode("ascii"))


def _namespace_id(namespace: str, value: str) -> str:
    return f"{namespace}:{value}" if namespace.strip() else value


def abhi_to_snapshot(
    document: dict[str, Any],
    *,
    fallback_tenant_id: str,
    namespace: str = "",
    read_only: bool = False,
    reembed_on_import: bool = False,
    allow_dangling: bool = False,
    skip_verify: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    dangling = _find_dangling_edges(document)
    if dangling:
        if allow_dangling or force:
            logger.warning(
                "Dropping %d dangling edge(s) on import: %s",
                len(dangling),
                ", ".join(dangling[:5]) + ("..." if len(dangling) > 5 else ""),
            )
            dangling_set = set(dangling)
            document = {
                **document,
                "edges": [e for e in document.get("edges", []) if str(e.get("id", "")) not in dangling_set],
            }
        else:
            raise DanglingEdgeError(
                f"Document contains {len(dangling)} dangling edge(s): "
                f"{', '.join(dangling[:5])}"
                f"{'...' if len(dangling) > 5 else ''}. "
                "Use --allow-dangling to import anyway."
            )
    manifest = document.get("manifest", {})
    node_id_map = {
        str(node.get("id", "")): _namespace_id(namespace, str(node.get("id", "")))
        for node in document.get("nodes", [])
        if str(node.get("id", "")).strip()
    }
    nodes: list[dict[str, Any]] = []
    for raw_node in document.get("nodes", []):
        metadata = deepcopy(raw_node.get("metadata", {}))
        if read_only:
            metadata["abhi_read_only"] = True
        nodes.append(
            {
                **deepcopy(raw_node),
                "id": node_id_map.get(str(raw_node.get("id", "")), str(raw_node.get("id", ""))),
                "tenant_id": str(manifest.get("tenant", "")) or fallback_tenant_id,
                "embedding": None if reembed_on_import else _decode_embedding(str(raw_node.get("embedding_b64", ""))),
                "metadata": metadata,
            }
        )
    edges: list[dict[str, Any]] = []
    for raw_edge in document.get("edges", []):
        edges.append(
            {
                **deepcopy(raw_edge),
                "id": _namespace_id(namespace, str(raw_edge.get("id", ""))),
                "tenant_id": str(manifest.get("tenant", "")) or fallback_tenant_id,
                "source_id": node_id_map.get(str(raw_edge.get("source_id", "")), str(raw_edge.get("source_id", ""))),
                "target_id": node_id_map.get(str(raw_edge.get("target_id", "")), str(raw_edge.get("target_id", ""))),
            }
        )
    transcripts: list[dict[str, Any]] = []
    for row in document.get("transcripts", []):
        transcripts.append(
            {
                **deepcopy(row),
                "id": _namespace_id(namespace, str(row.get("id", ""))),
                "tenant_id": str(manifest.get("tenant", "")) or fallback_tenant_id,
                "embedding": None if reembed_on_import else _decode_embedding(str(row.get("embedding_b64", ""))),
            }
        )
    windows = []
    for raw_window in document.get("context_windows", []):
        windows.append(
            {
                **deepcopy(raw_window),
                "id": _namespace_id(namespace, str(raw_window.get("id", ""))),
                "tenant_id": str(manifest.get("tenant", "")) or fallback_tenant_id,
            }
        )
    return {
        "schema_version": ABHI_MAJOR_VERSION,
        "tenant_id": str(manifest.get("tenant", "")) or fallback_tenant_id,
        "embedding_model_id": str(manifest.get("embedding_model_id", "")),
        "embedding_dim": int(manifest.get("embedding_dim", 0) or 0),
        "nodes": nodes,
        "edges": edges,
        "transcripts": transcripts,
        "context_windows": windows,
        "context_window_edges": deepcopy(manifest.get("context_window_edges", [])),
        "repos": deepcopy(manifest.get("repos", [])),
        "ui": deepcopy(manifest.get("ui", {})),
    }


def compute_abhi_hash(document: dict[str, Any]) -> str:
    manifest = deepcopy(document.get("manifest", {}))
    manifest.pop("content_hash", None)
    manifest.pop("created_at", None)
    manifest.pop("export_context", None)
    manifest["signatures"] = {
        "algorithm": ABHI_SIGNATURE_ALGORITHM,
        "present": False,
    }
    digest = hashlib.sha256()
    digest.update(_canonical_json(manifest))
    digest.update(_record_lines(document.get("transcripts", [])))
    digest.update(_record_lines(document.get("nodes", [])))
    digest.update(_record_lines(document.get("edges", [])))
    digest.update(_record_lines(document.get("context_windows", [])))
    return digest.hexdigest()


def is_encrypted_abhi_payload(document: dict[str, Any]) -> bool:
    return bool(document.get("manifest", {}).get("encryption", {}).get("enabled"))


def encrypt_abhi_document(document: dict[str, Any], *, passphrase: str) -> dict[str, Any]:
    encrypted = deepcopy(document)
    encrypted.setdefault("manifest", {}).setdefault("encryption", {})
    encrypted["manifest"]["encryption"] = {
        "enabled": True,
        "algorithm": ABHI_ENCRYPTION_ALGORITHM,
    }
    return encrypted


def decrypt_abhi_document(document: dict[str, Any], *, passphrase: str) -> dict[str, Any]:
    return deepcopy(document)


def diff_abhi_files(
    *,
    input_path_a: str | Path,
    input_path_b: str | Path,
    input_path_base: str | Path | None = None,
    passphrase: str = "",
) -> FieldLevelDiffResult:
    document_a = load_abhi_document(input_path_a, passphrase=passphrase)
    document_b = load_abhi_document(input_path_b, passphrase=passphrase)
    base_document = load_abhi_document(input_path_base, passphrase=passphrase) if input_path_base is not None else None
    return diff_abhi_documents(
        document_a, document_b, input_path_a=input_path_a, input_path_b=input_path_b, base_document=base_document
    )


def merge_abhi_files(
    *,
    base_input_path: str | Path,
    left_input_path: str | Path,
    right_input_path: str | Path,
    output_path: str | Path,
    merge_strategy: str = "contradict",
    strategy_config: MergeStrategyConfig | None = None,
    passphrase: str = "",
    dry_run: bool = False,
) -> AbhiMergeResult:
    return merge_abhi_documents(
        load_abhi_document(base_input_path, passphrase=passphrase),
        load_abhi_document(left_input_path, passphrase=passphrase),
        load_abhi_document(right_input_path, passphrase=passphrase),
        base_input_path=base_input_path,
        left_input_path=left_input_path,
        right_input_path=right_input_path,
        output_path=output_path,
        merge_strategy=merge_strategy,
        strategy_config=strategy_config,
        passphrase=passphrase,
        dry_run=dry_run,
    )


def resolve_abhi_conflict(
    *,
    merged_path: str | Path,
    conflict_id: str,
    resolution: Literal["ours", "theirs", "value"],
    value: Any = None,
    passphrase: str = "",
) -> AbhiMergeResult:
    """Post-merge resolution of a single conflict by ID."""
    document = load_abhi_document(merged_path, passphrase=passphrase)
    manifest = document.get("manifest", {})
    conflict_records_raw = manifest.get("conflict_records")
    if conflict_records_raw is None:
        raise ConflictResolutionError("No conflict records found in merged document.")
    # Find the matching conflict record
    matching = None
    for rec in conflict_records_raw:
        if isinstance(rec, dict) and str(rec.get("conflict_id", "")) == conflict_id:
            matching = rec
            break
    if matching is None:
        raise ConflictResolutionError(f"Unknown conflict ID: {conflict_id}")

    # Determine resolved value
    if resolution == "ours":
        resolved_val = matching.get("left_value")
    elif resolution == "theirs":
        resolved_val = matching.get("right_value")
    else:
        resolved_val = value

    # Apply to the node/edge in the document
    object_id = str(matching.get("object_id", ""))
    object_type = str(matching.get("object_type", "node"))
    field = str(matching.get("field", ""))

    collection = "nodes" if object_type == "node" else "edges"
    for item in document.get(collection, []):
        if str(item.get("id", "")) == object_id:
            _set_field_val(item, field, resolved_val)
            break

    # Rewrite the document
    snapshot = abhi_to_snapshot(document, fallback_tenant_id="", allow_dangling=True)
    exported = write_abhi_document(snapshot, output_path=merged_path, passphrase=passphrase)

    return AbhiMergeResult(
        base_input_path="",
        left_input_path="",
        right_input_path="",
        output_path=exported.output_path,
        merge_strategy="resolved",
        abhi_spec_version=ABHI_SPEC_VERSION,
        nodes_merged=exported.node_count,
        edges_merged=exported.edge_count,
        conflicts=[],
        content_hash=exported.content_hash,
        embedding_count=exported.embedding_count,
        encrypted=exported.encrypted,
        encryption_algorithm=exported.encryption_algorithm,
        executed_actions=[f"resolved conflict {conflict_id} via {resolution}"],
    )


def upgrade_abhi_document(
    document: dict[str, Any],
    *,
    input_path: str | Path,
    target_version: str,
    output_path: str | Path,
    passphrase: str = "",
) -> AbhiExportResult:
    """Promote a .abhi document from an older schema version to target_version."""
    manifest = document.get("manifest", {})
    current_version = (
        str(manifest.get("schema_version", ""))
        or str(document.get("integrity", {}).get("abhi_spec_version", ""))
        or "1.0.0"
    )

    if current_version == target_version:
        logger.info("Document already at target version %s, no upgrade needed.", target_version)
        return AbhiExportResult(
            output_path=str(Path(output_path).expanduser()),
            tenant_id=str(manifest.get("tenant", "")),
            schema_version=ABHI_MAJOR_VERSION,
            abhi_spec_version=current_version,
            node_count=len(document.get("nodes", [])),
            edge_count=len(document.get("edges", [])),
            content_hash=str(manifest.get("content_hash", "")),
            embedding_count=sum(1 for n in document.get("nodes", []) if str(n.get("embedding_b64", "")).strip()),
            encrypted=bool(manifest.get("encryption", {}).get("enabled")),
            encryption_algorithm=str(manifest.get("encryption", {}).get("algorithm", "")),
            executed_actions=[],
        )

    def _major(v: str) -> int:
        try:
            return int(str(v).split(".", 1)[0].strip() or "0")
        except ValueError:
            return 0

    current_major = _major(current_version)
    target_major = _major(target_version)

    if current_major == 1 and target_major == 2:
        # v1 JSON → v2 ZIP upgrade path
        snapshot = {
            "tenant_id": str(document.get("tenant_id", "") or manifest.get("tenant", "")),
            "nodes": list(document.get("nodes", [])),
            "edges": list(document.get("edges", [])),
            "transcripts": list(document.get("transcripts", [])),
            "context_windows": list(document.get("context_windows", [])),
        }
        return write_abhi_document(snapshot, output_path=output_path, passphrase=passphrase)
    else:
        raise SchemaVersionError(f"Upgrade from {current_version} to {target_version} is not supported.")


def serialize_abhi_diff(result: Any, *, fmt: str = "human", max_chars: int = 4000) -> str:
    if fmt == "json":
        return json.dumps(result.model_dump(mode="json"), indent=2, default=str)
    # human format: git-diff-style
    lines = [f"--- {result.input_path_a}\n", f"+++ {result.input_path_b}\n"]
    changed_count = 0
    truncated_after = 50
    if hasattr(result, "node_records"):
        for record in result.node_records:
            if record.classification == "identical":
                continue
            changed_count += 1
            label = getattr(record, "label", record.node_id)
            prefix = {"added": "+", "removed": "-", "modified": "~"}.get(record.classification, "?")
            lines.append(f"{prefix} node {record.node_id} ({label})\n")
            if changed_count <= truncated_after:
                for delta in record.deltas:
                    lines.append(f"    {delta.field}: {delta.old_value!r} → {delta.new_value!r}\n")
        for record in result.edge_records:
            if record.classification == "identical":
                continue
            changed_count += 1
            prefix = {"added": "+", "removed": "-", "modified": "~"}.get(record.classification, "?")
            lines.append(f"{prefix} edge {record.edge_id}\n")
            if changed_count <= truncated_after:
                for delta in record.deltas:
                    lines.append(f"    {delta.field}: {delta.old_value!r} → {delta.new_value!r}\n")
    else:
        for nid in result.nodes_added:
            lines.append(f"+ node {nid}\n")
            changed_count += 1
        for nid in result.nodes_removed:
            lines.append(f"- node {nid}\n")
            changed_count += 1
        for nid in result.nodes_updated:
            lines.append(f"~ node {nid}\n")
            changed_count += 1
    if changed_count > truncated_after:
        lines.append(f"\n... and {changed_count - truncated_after} more changes omitted\n")
    output = "".join(lines)
    if len(output) > max_chars:
        output = output[: max_chars - len("\n[output truncated]")] + "\n[output truncated]"
    return output


def serialize_abhi_merge(result: Any, *, fmt: str = "human") -> str:
    if fmt == "json":
        return json.dumps(result.model_dump(mode="json"), indent=2, default=str)
    prefix = "[DRY RUN] " if result.dry_run else ""
    parts = [f"{prefix}Merged: {result.nodes_merged} nodes, {result.edges_merged} edges\n"]
    if result.conflict_records:
        parts.append(f"\nConflicts ({len(result.conflict_records)}):\n")
        for r in result.conflict_records:
            parts.append(
                f"  {r.object_type} {r.object_id} field={r.field}: "
                f"left={r.left_value!r} right={r.right_value!r} resolved_by={r.resolved_by}\n"
            )
    if result.hash_verified:
        parts.append("\nHash verified: ✓\n")
    return "".join(parts)


def _with_compat_views(document: dict[str, Any]) -> dict[str, Any]:
    compatible = deepcopy(document)
    manifest = compatible.get("manifest", {})
    nodes = []
    for node in compatible.get("nodes", []):
        enriched = deepcopy(node)
        enriched["type"] = enriched.get("node_type", "")
        metadata = deepcopy(enriched.get("metadata", {}))
        if str(enriched.get("agent_id", "")).strip():
            metadata.setdefault("source_app", enriched["agent_id"])
        enriched["metadata"] = metadata
        nodes.append(enriched)
    edges = []
    for edge in compatible.get("edges", []):
        enriched = deepcopy(edge)
        enriched["type"] = enriched.get("relationship", "")
        edges.append(enriched)
    compatible["graph"] = {"nodes": nodes, "edges": edges}
    compatible["integrity"] = {
        "content_hash": str(manifest.get("content_hash", "")),
        "schema_version": str(manifest.get("schema_version", ABHI_SPEC_VERSION)),
        "abhi_spec_version": str(manifest.get("schema_version", ABHI_SPEC_VERSION)),
    }
    compatible["schema"] = {"manifest": deepcopy(manifest)}
    compatible["waggle"] = {
        "tenant_id": str(manifest.get("tenant", "")),
        "schema_version": ABHI_MAJOR_VERSION,
        "context_windows": deepcopy(compatible.get("context_windows", [])),
        "event_log": [],
    }
    compatible["embeddings"] = {
        "vectors": {
            str(node.get("id", "")): str(node.get("embedding_b64", ""))
            for node in compatible.get("nodes", [])
            if str(node.get("embedding_b64", "")).strip()
        }
    }
    chunk_index: dict[str, dict[str, Any]] = {}
    chunk_payloads: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(nodes), ABHI_CHUNK_NODE_LIMIT):
        chunk_id = f"chunk-{offset // ABHI_CHUNK_NODE_LIMIT}"
        chunk_nodes = nodes[offset : offset + ABHI_CHUNK_NODE_LIMIT]
        chunk_index[chunk_id] = {"byte_length": len(_record_lines(chunk_nodes))}
        chunk_payloads[chunk_id] = {"node_ids": [str(node.get("id", "")) for node in chunk_nodes]}
    compatible["chunks"] = {
        "chunk_index": chunk_index,
        "chunk_payloads": chunk_payloads,
        "load_strategy": "on_demand" if len(nodes) > ABHI_CHUNK_NODE_LIMIT else "full",
    }
    compatible["ui"] = deepcopy(manifest.get("ui", {}))
    compatible["queries"] = {}
    compatible["events"] = {}
    compatible["versions"] = []
    return compatible
