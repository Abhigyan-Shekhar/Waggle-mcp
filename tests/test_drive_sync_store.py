import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.oauth2.credentials import Credentials

from waggle.drive_sync import (
    FileCredentialStore,
    SecureCredentialStore,
    ensure_drive_credentials,
    is_secure_store_available,
)


@pytest.fixture
def temp_token_path(tmp_path):
    return tmp_path / "google-drive-token.json"


@pytest.fixture
def dummy_credentials():
    return Credentials(
        token="test-access-token",
        refresh_token="test-refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="test-client-id",
        client_secret="test-client-secret",
        scopes=["https://www.googleapis.com/auth/drive.file"],
        expiry=datetime.now(UTC) + timedelta(days=1),
    )


def test_file_credential_store_persistence(temp_token_path, dummy_credentials):
    store = FileCredentialStore(temp_token_path)
    assert store.load() is None

    store.save(dummy_credentials)
    assert temp_token_path.exists()

    loaded = store.load()
    assert loaded is not None
    assert loaded.token == "test-access-token"
    assert loaded.refresh_token == "test-refresh-token"
    assert loaded.client_id == "test-client-id"
    assert loaded.client_secret == "test-client-secret"

    store.delete()
    assert not temp_token_path.exists()
    assert store.load() is None


def test_secure_credential_store_persistence(temp_token_path, dummy_credentials):
    store = SecureCredentialStore(temp_token_path)

    fake_keyring_db = {}

    def mock_set_password(service, username, password):
        fake_keyring_db[(service, username)] = password

    def mock_get_password(service, username):
        return fake_keyring_db.get((service, username))

    def mock_delete_password(service, username):
        if (service, username) in fake_keyring_db:
            del fake_keyring_db[(service, username)]
        else:
            raise Exception("Password not found")

    with patch("keyring.set_password", side_effect=mock_set_password), \
         patch("keyring.get_password", side_effect=mock_get_password), \
         patch("keyring.delete_password", side_effect=mock_delete_password):

        assert store.load() is None
        store.save(dummy_credentials)

        loaded = store.load()
        assert loaded is not None
        assert loaded.token == "test-access-token"
        assert loaded.refresh_token == "test-refresh-token"
        assert loaded.client_id == "test-client-id"

        store.delete()
        assert store.load() is None


@pytest.mark.parametrize(
    "env_val, secure_available, expected_secure",
    [
        ("auto", True, True),
        ("auto", False, False),
        ("secure", True, True),
        ("file", True, False),
        ("file", False, False),
        ("", True, True),
    ]
)
def test_store_selection(env_val, secure_available, expected_secure, temp_token_path, dummy_credentials):
    env_mock = {"WAGGLE_DRIVE_CREDENTIAL_STORE": env_val}

    with patch("waggle.drive_sync.is_secure_store_available", return_value=secure_available), \
         patch.dict(os.environ, env_mock), \
         patch("waggle.drive_sync._run_local_oauth_flow", return_value=dummy_credentials):

        if env_val == "secure" and not secure_available:
            with pytest.raises(RuntimeError, match="Secure credential storage is requested"):
                ensure_drive_credentials(
                    client_secret_path="dummy_secret.json",
                    token_path=temp_token_path
                )
            return

        with patch.object(SecureCredentialStore, "load", return_value=None), \
             patch.object(SecureCredentialStore, "save") as mock_secure_save, \
             patch.object(FileCredentialStore, "load", return_value=None), \
             patch.object(FileCredentialStore, "save") as mock_file_save:

            ensure_drive_credentials(
                client_secret_path="dummy_secret.json",
                token_path=temp_token_path
            )

            if expected_secure:
                mock_secure_save.assert_called_once()
                mock_file_save.assert_not_called()
            else:
                mock_file_save.assert_called_once()
                mock_secure_save.assert_not_called()


def test_migration_path(temp_token_path, dummy_credentials):
    file_store = FileCredentialStore(temp_token_path)
    file_store.save(dummy_credentials)

    fake_keyring_db = {}
    def mock_set_password(service, username, password):
        fake_keyring_db[(service, username)] = password
    def mock_get_password(service, username):
        return fake_keyring_db.get((service, username))

    with patch("waggle.drive_sync.is_secure_store_available", return_value=True), \
         patch.dict(os.environ, {"WAGGLE_DRIVE_CREDENTIAL_STORE": "auto"}), \
         patch("keyring.set_password", side_effect=mock_set_password), \
         patch("keyring.get_password", side_effect=mock_get_password):

        creds = ensure_drive_credentials(
            client_secret_path="dummy_secret.json",
            token_path=temp_token_path
        )
        assert creds.token == "test-access-token"

        assert len(fake_keyring_db) == 1
        assert temp_token_path.exists()


def test_is_secure_store_available():
    from keyring.backends.fail import Keyring as FailKeyring
    with patch("keyring.get_keyring", return_value=FailKeyring()):
        assert not is_secure_store_available()

    with patch("builtins.__import__", side_effect=ImportError):
        assert not is_secure_store_available()

    mock_backend = MagicMock()
    mock_backend.__class__.__name__ = "WindowsCredentialManager"
    with patch("keyring.get_keyring", return_value=mock_backend):
        assert is_secure_store_available()
