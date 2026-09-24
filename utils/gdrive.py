"""Upload files to Google Drive.

Два режима аутентификации (приоритет — OAuth user token):
  1. OAuth user token: GDRIVE_TOKEN — путь к token.json от
     setup_gdrive_auth.py. Файлы принадлежат пользователю (его квота).
     Протухает при долгой неактивности (invalid_grant) — еженедельный
     прогон обновляет его refresh'ем.
  2. Service account: GDRIVE_SERVICE_ACCOUNT — путь к json сервисного
     аккаунта. ⚠️ Работает ТОЛЬКО с Shared Drives (Workspace): у сервисных
     аккаунтов нет квоты хранения, в обычный My Drive они заливать
     не могут (storageQuotaExceeded). Проверено 18.07.2026.

Required env vars:
  GDRIVE_FOLDER_ID  — ID of the Drive folder to upload into
                      (last part of https://drive.google.com/drive/folders/<ID>)
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_TOKEN = "token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def configured_token_path() -> str:
    """Return the OAuth token path, treating an empty env value as unset."""
    return (os.getenv("GDRIVE_TOKEN") or "").strip() or _DEFAULT_TOKEN


def _get_service():
    """Build authenticated Drive service: OAuth token, иначе service account."""
    from googleapiclient.discovery import build

    token_path = configured_token_path()
    sa_path = (os.getenv("GDRIVE_SERVICE_ACCOUNT") or "").strip()

    if not os.path.exists(token_path):
        if sa_path and os.path.exists(sa_path):
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                sa_path, scopes=SCOPES)
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        raise FileNotFoundError(
            f"[gdrive] token.json не найден: {token_path}. "
            "Запусти setup_gdrive_auth.py (или задай GDRIVE_SERVICE_ACCOUNT "
            "для Shared Drive)."
        )

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # A crash during refresh must not truncate the only refresh token.
            tmp_path = token_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, token_path)
        else:
            raise RuntimeError(
                f"[gdrive] token expired or missing refresh_token. "
                "Re-run setup_gdrive_auth.py to reauthorize."
            )

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(local_path: str, folder_id: str | None = None) -> str | None:
    """Upload a local file to Google Drive.

    Returns the web view URL on success, None on failure.
    Updates existing file with same name in folder, or creates new.
    """
    folder_id = folder_id or os.getenv("GDRIVE_FOLDER_ID", "")
    if not folder_id:
        print("[gdrive] GDRIVE_FOLDER_ID not set — skipping upload")
        return None

    try:
        from googleapiclient.http import MediaFileUpload

        service = _get_service()
        file_name = Path(local_path).name

        if local_path.endswith(".xlsx"):
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            mime = "text/csv"

        existing = (
            service.files()
            .list(
                q=f"name='{file_name}' and '{folder_id}' in parents and trashed=false",
                fields="files(id)",
            )
            .execute()
            .get("files", [])
        )

        media = MediaFileUpload(local_path, mimetype=mime, resumable=True)
        if existing:
            file_id = existing[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            meta = {"name": file_name, "parents": [folder_id]}
            result = service.files().create(
                body=meta, media_body=media, fields="id"
            ).execute()
            file_id = result["id"]

        link = f"https://drive.google.com/file/d/{file_id}/view"
        print(f"[gdrive] uploaded: {file_name} → {link}")
        return link

    except Exception as e:
        print(f"[gdrive] upload error for {local_path}: {e}")
        return None
