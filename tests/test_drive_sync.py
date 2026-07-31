from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.oauth2.credentials import Credentials

import waggle.drive_sync as drive_sync
from waggle.drive_sync import resolve_drive_file_id, share_drive_file
from waggle.models import DriveShareResult


@pytest.fixture
def mock_credentials():
    return MagicMock(spec=Credentials)


def test_resolve_drive_file_id_direct_id(mock_credentials):
    # Length >= 20, no slash, no space, should resolve directly
    ref = "abcdefghijklmnopqrstuvwxyz"
    with patch("waggle.drive_sync.build_drive_service") as mock_build:
        file_id, name = resolve_drive_file_id(file_ref=ref, credentials=mock_credentials)
        assert file_id == ref
        assert name == ""
        mock_build.assert_not_called()


def test_resolve_drive_file_id_empty_raises_value_error(mock_credentials):
    with pytest.raises(ValueError, match=r"Drive file reference cannot be empty\."):
        resolve_drive_file_id(file_ref="", credentials=mock_credentials)

    with pytest.raises(ValueError, match=r"Drive file reference cannot be empty\."):
        resolve_drive_file_id(file_ref="   ", credentials=mock_credentials)


@patch("waggle.drive_sync.build_drive_service")
def test_resolve_drive_file_id_filename_lookup(mock_build, mock_credentials):
    # A short ref or ref with spaces should trigger API lookup
    ref = "my file.txt"
    mock_service = MagicMock()
    mock_build.return_value = mock_service

    # Setup the mock API response hierarchy
    # service.files().list(q=...).execute() -> {"files": [{"id": "file123", "name": "my file.txt"}]}
    mock_files = MagicMock()
    mock_list_request = MagicMock()
    mock_service.files.return_value = mock_files
    mock_files.list.return_value = mock_list_request
    mock_list_request.execute.return_value = {"files": [{"id": "file123", "name": "my file.txt"}]}

    file_id, name = resolve_drive_file_id(file_ref=ref, credentials=mock_credentials)

    assert file_id == "file123"
    assert name == "my file.txt"
    mock_build.assert_called_once_with(credentials=mock_credentials)
    mock_files.list.assert_called_once_with(
        q="name = 'my file.txt' and trashed = false",
        pageSize=1,
        fields="files(id,name)",
    )


@patch("waggle.drive_sync.build_drive_service")
def test_resolve_drive_file_id_filename_lookup_with_folder_id(mock_build, mock_credentials):
    ref = "my file.txt"
    mock_service = MagicMock()
    mock_build.return_value = mock_service

    mock_files = MagicMock()
    mock_list_request = MagicMock()
    mock_service.files.return_value = mock_files
    mock_files.list.return_value = mock_list_request
    mock_list_request.execute.return_value = {"files": [{"id": "file123", "name": "my file.txt"}]}

    file_id, name = resolve_drive_file_id(
        file_ref=ref,
        credentials=mock_credentials,
        folder_id="folder-999",
    )

    assert file_id == "file123"
    assert name == "my file.txt"
    mock_files.list.assert_called_once_with(
        q="name = 'my file.txt' and trashed = false and 'folder-999' in parents",
        pageSize=1,
        fields="files(id,name)",
    )


@patch("waggle.drive_sync.build_drive_service")
def test_resolve_drive_file_id_escapes_single_quotes(mock_build, mock_credentials):
    ref = "O'Connor's File.txt"
    mock_service = MagicMock()
    mock_build.return_value = mock_service

    mock_files = MagicMock()
    mock_list_request = MagicMock()
    mock_service.files.return_value = mock_files
    mock_files.list.return_value = mock_list_request
    mock_list_request.execute.return_value = {
        "files": [{"id": "file456", "name": "O'Connor's File.txt"}],
    }

    file_id, name = resolve_drive_file_id(file_ref=ref, credentials=mock_credentials)

    assert file_id == "file456"
    assert name == "O'Connor's File.txt"
    # Verify escaping: O'Connor's -> O\'Connor\'s
    mock_files.list.assert_called_once_with(
        q="name = 'O\\'Connor\\'s File.txt' and trashed = false",
        pageSize=1,
        fields="files(id,name)",
    )


@patch("waggle.drive_sync.build_drive_service")
def test_resolve_drive_file_id_not_found_raises_value_error(mock_build, mock_credentials):
    ref = "nonexistent.txt"
    mock_service = MagicMock()
    mock_build.return_value = mock_service

    mock_files = MagicMock()
    mock_list_request = MagicMock()
    mock_service.files.return_value = mock_files
    mock_files.list.return_value = mock_list_request
    mock_list_request.execute.return_value = {"files": []}

    with pytest.raises(ValueError, match=r"No Drive file found for 'nonexistent\.txt'\."):
        resolve_drive_file_id(file_ref=ref, credentials=mock_credentials)


@patch("waggle.drive_sync.build_drive_service")
def test_share_drive_file(mock_build, mock_credentials):
    mock_service = MagicMock()
    mock_build.return_value = mock_service

    # Setup permissions create mock
    mock_permissions = MagicMock()
    mock_perm_request = MagicMock()
    mock_service.permissions.return_value = mock_permissions
    mock_permissions.create.return_value = mock_perm_request
    mock_perm_request.execute.return_value = {"id": "permission-abc"}

    # Setup files get mock
    mock_files = MagicMock()
    mock_get_request = MagicMock()
    mock_service.files.return_value = mock_files
    mock_files.get.return_value = mock_get_request
    mock_get_request.execute.return_value = {
        "id": "file-xyz",
        "webViewLink": "https://drive.google.com/file/xyz/view",
    }

    result = share_drive_file(file_id="file-xyz", credentials=mock_credentials)

    assert isinstance(result, DriveShareResult)
    assert result.remote_file_id == "file-xyz"
    assert result.permission_id == "permission-abc"
    assert result.web_view_link == "https://drive.google.com/file/xyz/view"

    mock_permissions.create.assert_called_once_with(
        fileId="file-xyz", body={"type": "anyone", "role": "reader"}, fields="id"
    )
    mock_files.get.assert_called_once_with(fileId="file-xyz", fields="id,webViewLink")


def make_drive_service(
    *,
    response: dict[str, str] | None = None,
) -> tuple[MagicMock, MagicMock]:
    service = MagicMock()
    request = MagicMock()

    service.files.return_value.create.return_value = request
    request.execute.return_value = response or {
        "id": "drive-file-id",
        "name": "backup.abhi",
        "webViewLink": "https://drive.google.com/file/d/drive-file-id/view",
    }

    return service, request


def patch_drive_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    service: MagicMock,
) -> tuple[MagicMock, object]:
    build_service = MagicMock(return_value=service)
    media = object()
    media_factory = MagicMock(return_value=media)

    monkeypatch.setattr(
        drive_sync,
        "build_drive_service",
        build_service,
    )
    monkeypatch.setattr(
        drive_sync,
        "MediaFileUpload",
        media_factory,
    )

    return media_factory, media


def test_small_file_uses_simple_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_path = tmp_path / "small.abhi"
    upload_path.write_bytes(b"small upload")

    service, request = make_drive_service()
    media_factory, media = patch_drive_dependencies(
        monkeypatch,
        service,
    )

    result = drive_sync.push_file_to_drive(
        local_path=upload_path,
        folder_id="folder-id",
        credentials=MagicMock(),
        remote_name="backup.abhi",
        encrypted=True,
        resumable_threshold_bytes=1024,
    )

    media_factory.assert_called_once_with(
        str(upload_path),
        mimetype="application/zip",
        resumable=False,
    )

    service.files.return_value.create.assert_called_once_with(
        body={
            "name": "backup.abhi",
            "parents": ["folder-id"],
        },
        media_body=media,
        fields="id,name,webViewLink",
    )

    request.execute.assert_called_once_with(num_retries=3)
    request.next_chunk.assert_not_called()

    assert result.local_path == str(upload_path)
    assert result.remote_file_id == "drive-file-id"
    assert result.remote_name == "backup.abhi"
    assert result.folder_id == "folder-id"
    assert result.web_view_link == ("https://drive.google.com/file/d/drive-file-id/view")
    assert result.encrypted is True


def test_large_file_uses_resumable_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_path = tmp_path / "large.abhi"
    upload_path.write_bytes(b"large")

    service, request = make_drive_service()
    media_factory, _ = patch_drive_dependencies(
        monkeypatch,
        service,
    )

    complete_status = MagicMock()
    complete_status.progress.return_value = 1.0

    request.next_chunk.return_value = (
        complete_status,
        {
            "id": "drive-file-id",
            "name": "large.abhi",
            "webViewLink": "https://drive.example/large",
        },
    )

    drive_sync.push_file_to_drive(
        local_path=upload_path,
        folder_id="",
        credentials=MagicMock(),
        resumable_threshold_bytes=1,
        chunk_size=4 * 1024 * 1024,
    )

    media_factory.assert_called_once_with(
        str(upload_path),
        mimetype="application/zip",
        chunksize=4 * 1024 * 1024,
        resumable=True,
    )

    request.next_chunk.assert_called_once_with(num_retries=3)
    request.execute.assert_not_called()


def test_resumable_upload_reports_chunk_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_path = tmp_path / "progress.abhi"
    upload_path.write_bytes(b"progress")

    service, request = make_drive_service()
    patch_drive_dependencies(monkeypatch, service)

    halfway = MagicMock()
    halfway.progress.return_value = 0.5

    complete = MagicMock()
    complete.progress.return_value = 1.0

    request.next_chunk.side_effect = [
        (halfway, None),
        (
            complete,
            {
                "id": "drive-file-id",
                "name": "progress.abhi",
                "webViewLink": "https://drive.example/progress",
            },
        ),
    ]

    progress_updates: list[int] = []

    drive_sync.push_file_to_drive(
        local_path=upload_path,
        folder_id="",
        credentials=MagicMock(),
        resumable_threshold_bytes=1,
        progress_callback=progress_updates.append,
    )

    assert progress_updates == [50, 100]
    assert request.next_chunk.call_count == 2

    for call in request.next_chunk.call_args_list:
        assert call.kwargs == {"num_retries": 3}


def test_resumable_upload_does_not_repeat_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_path = tmp_path / "duplicate-progress.abhi"
    upload_path.write_bytes(b"progress")

    service, request = make_drive_service()
    patch_drive_dependencies(monkeypatch, service)

    first = MagicMock()
    first.progress.return_value = 0.25

    duplicate = MagicMock()
    duplicate.progress.return_value = 0.25

    complete = MagicMock()
    complete.progress.return_value = 1.0

    request.next_chunk.side_effect = [
        (first, None),
        (duplicate, None),
        (
            complete,
            {
                "id": "drive-file-id",
                "name": "duplicate-progress.abhi",
                "webViewLink": "",
            },
        ),
    ]

    updates: list[int] = []

    drive_sync.push_file_to_drive(
        local_path=upload_path,
        folder_id="",
        credentials=MagicMock(),
        resumable_threshold_bytes=1,
        progress_callback=updates.append,
    )

    assert updates == [25, 100]


def test_threshold_boundary_uses_resumable_upload(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "boundary.abhi"
    upload_path.write_bytes(b"12345")

    assert drive_sync.should_use_resumable_upload(
        upload_path,
        threshold_bytes=5,
    )


def test_missing_file_fails_before_service_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_service = MagicMock()

    monkeypatch.setattr(
        drive_sync,
        "build_drive_service",
        build_service,
    )

    with pytest.raises(
        FileNotFoundError,
        match="Drive upload file not found",
    ):
        drive_sync.push_file_to_drive(
            local_path=tmp_path / "missing.abhi",
            folder_id="",
            credentials=MagicMock(),
        )

    build_service.assert_not_called()


def test_resumable_upload_failure_has_useful_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload_path = tmp_path / "failed.abhi"
    upload_path.write_bytes(b"failed")

    service, request = make_drive_service()
    patch_drive_dependencies(monkeypatch, service)

    request.next_chunk.side_effect = OSError("connection reset")

    with pytest.raises(
        RuntimeError,
        match=r"Google Drive resumable upload failed.*connection reset",
    ):
        drive_sync.push_file_to_drive(
            local_path=upload_path,
            folder_id="",
            credentials=MagicMock(),
            resumable_threshold_bytes=1,
        )


def test_invalid_upload_options_are_rejected(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "options.abhi"
    upload_path.write_bytes(b"options")

    with pytest.raises(
        ValueError,
        match="chunk_size must be positive",
    ):
        drive_sync.push_file_to_drive(
            local_path=upload_path,
            folder_id="",
            credentials=MagicMock(),
            chunk_size=0,
        )

    with pytest.raises(
        ValueError,
        match="max_retries cannot be negative",
    ):
        drive_sync.push_file_to_drive(
            local_path=upload_path,
            folder_id="",
            credentials=MagicMock(),
            max_retries=-1,
        )

    with pytest.raises(
        ValueError,
        match="threshold_bytes cannot be negative",
    ):
        drive_sync.push_file_to_drive(
            local_path=upload_path,
            folder_id="",
            credentials=MagicMock(),
            resumable_threshold_bytes=-1,
        )


def test_invalid_resumable_chunk_size_alignment_is_rejected(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / "misaligned.abhi"
    upload_path.write_bytes(b"misaligned")

    with pytest.raises(
        ValueError,
        match="chunk_size must be a multiple of 256 KiB",
    ):
        drive_sync.push_file_to_drive(
            local_path=upload_path,
            folder_id="",
            credentials=MagicMock(),
            chunk_size=1000,
            resumable_threshold_bytes=1,
        )
