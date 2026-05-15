"""
One-time Gmail OAuth2 setup.
Run once: python -m tracking.gmail_setup
Opens browser → grant read-only Gmail access → saves token to credentials/gmail_token.json
"""
from pathlib import Path

_SCOPES      = ["https://www.googleapis.com/auth/gmail.readonly"]
_CLIENT_FILE = Path("credentials/drive_oauth_client.json")
_TOKEN_FILE  = Path("credentials/gmail_token.json")


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not _CLIENT_FILE.exists():
        print(f"ERROR: {_CLIENT_FILE} not found.")
        print("Download OAuth client JSON from Google Cloud Console → APIs & Services → Credentials")
        return

    print("Opening browser for Gmail read-only access…")
    flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT_FILE), _SCOPES)
    creds = flow.run_local_server(port=0)
    _TOKEN_FILE.write_text(creds.to_json())
    print(f"✅ Gmail token saved to {_TOKEN_FILE}")
    print("Bot will now track job application replies automatically.")


if __name__ == "__main__":
    main()
