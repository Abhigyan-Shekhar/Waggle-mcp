from unittest.mock import MagicMock, patch

import pytest
from google.oauth2.credentials import Credentials

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

    file_id, name = resolve_drive_file_id(file_ref=ref, credentials=mock_credentials, folder_id="folder-999")

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
    mock_list_request.execute.return_value = {"files": [{"id": "file456", "name": "O'Connor's File.txt"}]}

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
