#!/usr/bin/env python3
"""One-time OAuth2 authorization for Google Drive.

Usage:
  1. Download client_secrets.json from Google Cloud Console
     (APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON)
  2. Run: python setup_gdrive_auth.py client_secrets.json
  3. Browser opens → grant access to your Google account
  4. token.json is saved in the current directory
  5. Upload token.json to the server (script will show the command)

Requirements:
  pip install google-auth-oauthlib
"""
import json
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_PATH = "token.json"


def main() -> None:
    secrets_path = sys.argv[1] if len(sys.argv) > 1 else "client_secrets.json"

    if not os.path.exists(secrets_path):
        print(f"ERROR: {secrets_path} not found.")
        print()
        print("Steps to create it:")
        print("  1. Open https://console.cloud.google.com/")
        print("  2. Create or select a project (e.g. 'b2b-parsing')")
        print("  3. APIs & Services → Credentials")
        print("  4. + CREATE CREDENTIALS → OAuth client ID")
        print("  5. Application type: Desktop app")
        print("  6. Click CREATE, then DOWNLOAD JSON")
        print(f"  7. Save as: {secrets_path}")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib not installed.")
        print("Run: pip install google-auth-oauthlib")
        sys.exit(1)

    print(f"Using client secrets: {secrets_path}")
    print("Opening browser for authorization...")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    token_data = json.loads(creds.to_json())
    has_refresh = bool(token_data.get("refresh_token"))

    print()
    print(f"✅ Authorization successful!")
    print(f"   Token saved to: {TOKEN_PATH}")
    print(f"   Refresh token present: {has_refresh}")
    print()
    print("Upload to server:")
    print(f"   scp {TOKEN_PATH} root@<server-ip>:/home/b2b_parser/token.json")


if __name__ == "__main__":
    main()
