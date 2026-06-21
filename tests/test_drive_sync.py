"""Tests for Google Drive helper functions in waggle.drive_sync."""

from unittest.mock import MagicMock, patch

import pytest

from waggle.drive_sync import (
    create_share_link,
    query_escape,
    resolve_drive_file_id,
)


class TestResolveDriveFileId:
    """Tests for resolve_drive_file_id()."""

    def test_resolve_by_direct_id(self):
        """Should return the ID directly if it looks like a file ID."""
        result = resolve_drive_file_id(None, "abc123_XYZ")
        assert result == "abc123_XYZ"

    def test_resolve_by_filename_found(self):
        """Should return the file ID when filename matches."""
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_list = MagicMock()
        mock_execute = MagicMock()

        mock_service.files.return_value = mock_files
        mock_files.list.return_value = mock_list
        mock_list.execute.return_value = mock_execute
        mock_execute.get.return_value = [{"id": "file123"}]

        result = resolve_drive_file_id(mock_service, "my_doc.txt")
        assert result == "file123"

        # Verify the query was constructed correctly
        call_kwargs = mock_list.call_args[1]
        assert "name = 'my_doc.txt'" in call_kwargs["q"]
        assert call_kwargs["spaces"] == "drive"
        assert call_kwargs["fields"] == "files(id, name)"

    def test_resolve_by_filename_not_found(self):
        """Should return None when no file matches."""
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_list = MagicMock()
        mock_execute = MagicMock()

        mock_service.files.return_value = mock_files
        mock_files.list.return_value = mock_list
        mock_list.execute.return_value = mock_execute
        mock_execute.get.return_value = []

        result = resolve_drive_file_id(mock_service, "nonexistent.txt")
        assert result is None

    def test_resolve_by_filename_api_error(self):
        """Should raise the API error when the request fails."""
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_list = MagicMock()

        mock_service.files.return_value = mock_files
        mock_files.list.return_value = mock_list
        mock_list.execute.side_effect = Exception("API error")

        with pytest.raises(Exception, match="API error"):
            resolve_drive_file_id(mock_service, "broken.txt")


class TestQueryEscape:
    """Tests for query_escape()."""

    def test_basic_string(self):
        """Should escape a simple string correctly."""
        result = query_escape("hello world")
        assert result == "'hello world'"

    def test_special_characters(self):
        """Should escape quotes and backslashes."""
        result = query_escape("it's a \"test\"\\file")
        # Single quotes are escaped by doubling, backslashes are escaped
        expected = "'it''s a \"test\"\\file'"
        assert result == expected

    def test_unicode_characters(self):
        """Should preserve unicode characters."""
        result = query_escape("café résumé")
        assert result == "'café résumé'"


class TestCreateShareLink:
    """Tests for create_share_link()."""

    def test_create_share_link_success(self):
        """Should create a permission and return the webViewLink."""
        mock_service = MagicMock()
        mock_permissions = MagicMock()
        mock_create = MagicMock()
        mock_result = MagicMock()

        mock_service.permissions.return_value = mock_permissions
        mock_permissions.create.return_value = mock_create
        mock_create.execute.return_value = mock_result
        mock_result.get.return_value = "https://drive.google.com/file/d/abc123/view"

        result = create_share_link(mock_service, "abc123")
        assert result == "https://drive.google.com/file/d/abc123/view"

        # Verify permission creation call
        call_kwargs = mock_permissions.create.call_args[1]
        assert call_kwargs["fileId"] == "abc123"
        assert call_kwargs["body"]["role"] == "reader"
        assert call_kwargs["body"]["type"] == "anyone"
        assert call_kwargs["fields"] == "id"

    def test_create_share_link_permission_error(self):
        """Should raise when permission creation fails."""
        mock_service = MagicMock()
        mock_permissions = MagicMock()
        mock_create = MagicMock()

        mock_service.permissions.return_value = mock_permissions
        mock_permissions.create.return_value = mock_create
        mock_create.execute.side_effect = Exception("Permission error")

        with pytest.raises(Exception, match="Permission error"):
            create_share_link(mock_service, "abc123")

    def test_create_share_link_webview_link(self):
        """Should return the correct webViewLink format."""
        mock_service = MagicMock()
        mock_permissions = MagicMock()
        mock_create = MagicMock()
        mock_result = MagicMock()

        mock_service.permissions.return_value = mock_permissions
        mock_permissions.create.return_value = mock_create
        mock_create.execute.return_value = mock_result
        mock_result.get.return_value = "https://drive.google.com/file/d/xyz789/view"

        result = create_share_link(mock_service, "xyz789")
        assert "https://drive.google.com/file/d/" in result
        assert "xyz789" in result