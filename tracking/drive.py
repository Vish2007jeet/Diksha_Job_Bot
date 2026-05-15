"""
Google Drive uploader for job application documents.

After every CV/CL generation, uploads the full application folder to Drive:
  Google Drive / Job_Bot_Applications / <N>. <Company>_<Type> /
      CV_{name}.docx
      CV_{name}.pdf
      CL_{name}.docx
      CL_{name}.pdf

Uses OAuth2 user credentials (not service account) so files are owned by you
and count against your Drive quota, not the service account's non-existent quota.

Setup (one-time):
  1. Go to console.cloud.google.com → create or select your GCP project
  2. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
  3. Application type: Desktop app  (name: "Job Bot Drive")
  4. Download JSON → save as  credentials/drive_oauth_client.json
  5. Run:  python -m tracking.drive_setup
     → Browser opens once, you log in, token saved to credentials/drive_token.json
  6. All future uploads happen automatically.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import List, Optional

import config
from utils.logger import logger

SCOPES = ["https://www.googleapis.com/auth/drive"]
_FOLDER_MIME = "application/vnd.google-apps.folder"

# Paths for OAuth2 credentials
_CLIENT_SECRET = config.BASE_DIR / "credentials" / "drive_oauth_client.json"
_TOKEN_FILE    = config.BASE_DIR / "credentials" / "drive_token.json"


class DriveUploader:
    """
    Uploads application documents to a Google Drive folder using OAuth2
    user credentials.  Gracefully no-ops if credentials or folder ID are
    not configured.
    """

    def __init__(self):
        self._service = None
        self._enabled = bool(
            config.GOOGLE_DRIVE_FOLDER_ID
            and (_TOKEN_FILE.exists() or _CLIENT_SECRET.exists())
        )
        if not self._enabled:
            logger.info(
                "Google Drive upload disabled. "
                "See credentials/drive_oauth_client.json setup instructions."
            )

    # ── Public API ─────────────────────────────────────────────

    def upload_application(
        self,
        folder_name: str,
        file_paths: List[str],
    ) -> Optional[str]:
        """
        Create a subfolder named `folder_name` inside the root Drive folder,
        upload all files in `file_paths` to it, and return the folder URL.
        Returns None on failure or if Drive is not configured.
        """
        if not self._enabled:
            return None
        try:
            svc = self._get_service()
            subfolder_id = self._get_or_create_folder(
                svc, folder_name, config.GOOGLE_DRIVE_FOLDER_ID
            )
            for path_str in file_paths:
                p = Path(path_str)
                if p.exists():
                    self._upload_file(svc, p, subfolder_id)
                    logger.info(f"Drive: uploaded {p.name} → {folder_name}/")
            folder_url = f"https://drive.google.com/drive/folders/{subfolder_id}"
            logger.info(f"Drive folder ready: {folder_url}")
            return folder_url
        except Exception as exc:
            logger.warning(f"Drive upload failed: {exc}")
            return None

    # ── Private helpers ────────────────────────────────────────

    def _get_service(self):
        if self._service is not None:
            return self._service

        from googleapiclient.discovery import build
        creds = self._load_credentials()
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _load_credentials(self):
        """Load saved OAuth2 token, refreshing if needed. Run drive_setup first."""
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        import json

        if _TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    _TOKEN_FILE.write_text(creds.to_json())
                    logger.info("Drive: OAuth2 token refreshed.")
                    return creds
                except Exception as exc:
                    err = str(exc)
                    if "invalid_grant" in err:
                        msg = (
                            "⚠️ <b>Google Drive token revoked</b>\n\n"
                            "The Drive OAuth refresh token has expired (7-day limit in GCP Testing mode).\n\n"
                            "▶️ Fix in two steps:\n"
                            "1. <b>Permanent fix</b> — publish your GCP OAuth consent screen:\n"
                            "   console.cloud.google.com → APIs &amp; Services → OAuth consent screen → Publish\n\n"
                            "2. <b>Re-authorise Drive</b> in your terminal:\n"
                            "<code>cd D:\\Job_Bot\n.venv\\Scripts\\python.exe -m tracking.drive_setup</code>"
                        )
                        alert_file = config.BASE_DIR / "data" / "pending_alert.json"
                        alert_file.parent.mkdir(exist_ok=True)
                        alert_file.write_text(
                            json.dumps({"message": msg, "sent": False}, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        logger.warning("Drive: invalid_grant — token revoked. Re-run drive_setup.")
                    elif "invalid_scope" in err:
                        msg = (
                            "⚠️ <b>Google Drive scope mismatch</b>\n\n"
                            "The saved Drive token was authorised with different scopes and can no longer refresh.\n\n"
                            "▶️ Fix: delete the old token and re-authorise:\n"
                            "<code>cd D:\\Job_Bot\n"
                            "del credentials\\drive_token.json\n"
                            ".venv\\Scripts\\python.exe -m tracking.drive_setup</code>"
                        )
                        alert_file = config.BASE_DIR / "data" / "pending_alert.json"
                        alert_file.parent.mkdir(exist_ok=True)
                        alert_file.write_text(
                            json.dumps({"message": msg, "sent": False}, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        logger.warning("Drive: invalid_scope — token scope mismatch. Re-run drive_setup.")
                    raise  # propagates to upload_application → logs warning, returns None

        # Token missing or invalid — fall back to interactive flow if client secret exists
        if _CLIENT_SECRET.exists():
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
            _TOKEN_FILE.write_text(creds.to_json())
            logger.info("Drive: new OAuth2 token saved.")
            return creds

        raise RuntimeError(
            "No Drive credentials found. "
            "Download credentials/drive_oauth_client.json from GCP Console "
            "then run: python -m tracking.drive_setup"
        )

    def _get_or_create_folder(self, svc, name: str, parent_id: str) -> str:
        """Return existing subfolder ID or create it."""
        q = (
            f"name='{name}' "
            f"and '{parent_id}' in parents "
            f"and mimeType='{_FOLDER_MIME}' "
            f"and trashed=false"
        )
        res = svc.files().list(q=q, fields="files(id,name)").execute()
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        meta = {
            "name": name,
            "mimeType": _FOLDER_MIME,
            "parents": [parent_id],
        }
        folder = svc.files().create(body=meta, fields="id").execute()
        return folder["id"]

    def _upload_file(self, svc, path: Path, parent_id: str) -> None:
        """Upload a file to Drive, replacing any existing file with the same name."""
        from googleapiclient.http import MediaFileUpload

        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

        q = f"name='{path.name}' and '{parent_id}' in parents and trashed=false"
        res = svc.files().list(q=q, fields="files(id)").execute()
        existing = res.get("files", [])

        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=False)
        if existing:
            svc.files().update(
                fileId=existing[0]["id"],
                media_body=media,
            ).execute()
        else:
            meta = {"name": path.name, "parents": [parent_id]}
            svc.files().create(body=meta, media_body=media, fields="id").execute()
