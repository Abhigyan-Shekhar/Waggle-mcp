from __future__ import annotations

import logging

from waggle.errors import ValidationFailure

LOGGER = logging.getLogger(__name__)
_DRIVE_SYNC_IMPORT_ERROR: Exception | None = None

try:
    from waggle.drive_sync import (
        download_drive_file,
        ensure_drive_credentials,
        merge_downloaded_abhi,
        push_file_to_drive,
        resolve_drive_file_id,
        share_drive_file,
    )
except Exception as exc:  # pragma: no cover - depends on optional Google libs
    download_drive_file = None
    ensure_drive_credentials = None
    merge_downloaded_abhi = None
    push_file_to_drive = None
    resolve_drive_file_id = None
    share_drive_file = None
    _DRIVE_SYNC_IMPORT_ERROR = exc


def _require_drive_sync() -> None:
    if _DRIVE_SYNC_IMPORT_ERROR is None:
        return
    raise ValidationFailure(
        "Google Drive sync requires optional Google API dependencies in the active environment. "
        f"Original import error: {_DRIVE_SYNC_IMPORT_ERROR}"
    )
