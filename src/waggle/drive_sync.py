from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from waggle.abhi import (
    ABHI_MERGE_STRATEGIES,
    DEFAULT_ABHI_MERGE_STRATEGY,
    merge_abhi_documents,
)
from waggle.errors import ValidationFailure
from waggle.models import DrivePushResult, DriveShareResult

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def ensure_drive_credentials(
    *,
    client_secret_path: str | Path,
    token_path: str | Path,
    scopes: list[str] | None = None,
    open_browser: bool = True,
) -> Credentials:
    scopes = scopes or [DRIVE_SCOPE]
    token_file = Path(token_path).expanduser()
    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), scopes=scopes)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_file.write_text(credentials.to_json(), encoding="utf-8")
        if credentials.valid:
            return credentials
    credentials = _run_local_oauth_flow(
        client_secret_path=client_secret_path,
        scopes=scopes,
        open_browser=open_browser,
    )
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def build_drive_service(*, credentials: Credentials) -> Any:
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def push_file_to_drive(
    *,
    local_path: str | Path,
    folder_id: str,
    credentials: Credentials,
    remote_name: str = "",
    encrypted: bool = False,
) -> DrivePushResult:
    path = Path(local_path).expanduser()
    service = build_drive_service(credentials=credentials)
    metadata: dict[str, Any] = {"name": remote_name or path.name}
    if folder_id.strip():
        metadata["parents"] = [folder_id.strip()]
    created = (
        service.files()
        .create(
            body=metadata,
            media_body=MediaFileUpload(str(path), mimetype="application/zip", resumable=False),
            fields="id,name,webViewLink",
        )
        .execute()
    )
    return DrivePushResult(
        local_path=str(path),
        remote_file_id=str(created.get("id", "")),
        remote_name=str(created.get("name", "")),
        folder_id=folder_id,
        web_view_link=str(created.get("webViewLink", "")),
        encrypted=encrypted,
    )


def download_drive_file(
    *,
    file_id: str,
    destination_path: str | Path,
    credentials: Credentials,
) -> tuple[str, str]:
    destination = Path(destination_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    service = build_drive_service(credentials=credentials)
    file_meta = service.files().get(fileId=file_id, fields="id,name").execute()
    request = service.files().get_media(fileId=file_id)
    with destination.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return str(file_meta.get("id", "")), str(file_meta.get("name", ""))


def resolve_drive_file_id(*, file_ref: str, credentials: Credentials, folder_id: str = "") -> tuple[str, str]:
    if not file_ref.strip():
        raise ValueError("Drive file reference cannot be empty.")
    if "/" not in file_ref and " " not in file_ref and len(file_ref) >= 20:
        return file_ref.strip(), ""
    service = build_drive_service(credentials=credentials)
    name = file_ref.strip().replace("'", "\\'")
    query = f"name = '{name}' and trashed = false"
    if folder_id.strip():
        query += f" and '{folder_id.strip()}' in parents"
    result = service.files().list(q=query, pageSize=1, fields="files(id,name)").execute()
    files = result.get("files", [])
    if not files:
        raise ValueError(f"No Drive file found for '{file_ref}'.")
    return str(files[0].get("id", "")), str(files[0].get("name", ""))


def share_drive_file(*, file_id: str, credentials: Credentials) -> DriveShareResult:
    service = build_drive_service(credentials=credentials)
    permission = (
        service.permissions()
        .create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        )
        .execute()
    )
    file_meta = service.files().get(fileId=file_id, fields="id,webViewLink").execute()
    return DriveShareResult(
        remote_file_id=str(file_meta.get("id", "")),
        permission_id=str(permission.get("id", "")),
        web_view_link=str(file_meta.get("webViewLink", "")),
    )


def merge_downloaded_abhi(
    *,
    local_document: dict[str, Any],
    remote_document: dict[str, Any],
    output_path: str | Path,
    merge_strategy: str = DEFAULT_ABHI_MERGE_STRATEGY,
) -> str:
    """Merge a downloaded remote .abhi document with the local document.

    Args:
        local_document: The current local .abhi document.
        remote_document: The downloaded remote .abhi document.
        output_path: Destination path for the merged .abhi file.
        merge_strategy: Conflict-resolution strategy used when both documents
            modify the same object. Supported values are contradict,
            last_write_wins, prefer_left, and prefer_right.

    Returns:
        The path to the written merged .abhi file.

    Raises:
        ValidationFailure: If merge_strategy is unsupported.
    """
    normalized_merge_strategy = str(merge_strategy).strip()
    if normalized_merge_strategy not in ABHI_MERGE_STRATEGIES:
        supported = ", ".join(ABHI_MERGE_STRATEGIES)
        raise ValidationFailure(f"Invalid merge_strategy {merge_strategy!r}. Expected one of: {supported}.")

    merged = merge_abhi_documents(
        {
            "graph": {"nodes": [], "edges": []},
            "schema": {},
            "constraints": [],
            "ai_rules": {},
            "ui": {},
            "external_refs": [],
            "chunks": {},
            "embeddings": {"vectors": {}},
            "queries": {},
            "events": {},
            "versions": [],
            "waggle": {},
            "integrity": {},
        },
        local_document,
        remote_document,
        base_input_path="local://empty",
        left_input_path="local://current",
        right_input_path="local://remote",
        output_path=output_path,
        merge_strategy=normalized_merge_strategy,
    )

    return merged.output_path


def _run_local_oauth_flow(
    *,
    client_secret_path: str | Path,
    scopes: list[str],
    open_browser: bool,
) -> Credentials:
    config = json.loads(Path(client_secret_path).expanduser().read_text(encoding="utf-8"))
    client_info = config.get("installed") or config.get("web") or {}
    client_id = str(client_info.get("client_id", "")).strip()
    client_secret = str(client_info.get("client_secret", "")).strip()
    redirect_uris = list(client_info.get("redirect_uris") or [])
    redirect_uri = next(
        (uri for uri in redirect_uris if uri.startswith(("http://127.0.0.1", "http://localhost"))),
        "http://127.0.0.1:8765/",
    )
    parsed = urllib.parse.urlparse(redirect_uri)
    code_verifier = secrets.token_urlsafe(48)
    code_challenge = _pkce_challenge(code_verifier)
    state = secrets.token_urlsafe(24)
    code_holder: dict[str, str] = {}
    server = _OAuthCallbackServer(
        parsed.hostname or "127.0.0.1", parsed.port or 8765, parsed.path or "/", state, code_holder
    )
    thread = threading.Thread(target=server.serve_once, daemon=True)
    thread.start()
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    if open_browser:
        webbrowser.open(auth_url)
    else:
        raise RuntimeError(f"Open this URL to authorize Waggle Drive access: {auth_url}")
    thread.join(timeout=300)
    code = code_holder.get("code", "")
    if not code:
        raise RuntimeError("Timed out waiting for Google OAuth callback.")
    token_response = requests.post(
        TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    token_response.raise_for_status()
    payload = token_response.json()
    return Credentials(
        token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class _OAuthCallbackServer:
    def __init__(self, host: str, port: int, path: str, state: str, code_holder: dict[str, str]) -> None:
        self.host = host
        self.port = port
        self.path = path
        self.state = state
        self.code_holder = code_holder

    def serve_once(self) -> None:
        expected_path = self.path
        expected_state = self.state
        code_holder = self.code_holder

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                if parsed.path != expected_path or params.get("state", [""])[0] != expected_state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Invalid OAuth callback.")
                    return
                code_holder["code"] = params.get("code", [""])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Waggle Drive authentication complete. You can close this window.")

            def log_message(self, format: str, *args: object) -> None:
                return

        with HTTPServer((self.host, self.port), Handler) as httpd:
            httpd.handle_request()
