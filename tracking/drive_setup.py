"""
One-time Google Drive OAuth2 setup.

Run once from the project root:
    python -m tracking.drive_setup

Prerequisites:
  1. Go to console.cloud.google.com → create or select your GCP project
  2. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
  3. Application type: Desktop app  (name: "Job Bot Drive")
  4. Download JSON → save as  credentials/drive_oauth_client.json
  5. Run this script — a browser opens, you approve, token is saved.
"""
from pathlib import Path

BASE_DIR       = Path(__file__).parent.parent
CLIENT_SECRET  = BASE_DIR / "credentials" / "drive_oauth_client.json"
TOKEN_FILE     = BASE_DIR / "credentials" / "drive_token.json"
SCOPES         = ["https://www.googleapis.com/auth/drive"]


def main():
    if not CLIENT_SECRET.exists():
        print(f"ERROR: {CLIENT_SECRET} not found.")
        print("Download your OAuth 2.0 client secret from GCP Console:")
        print("  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON")
        print(f"Save it as: {CLIENT_SECRET}")
        return

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("Opening browser for Google Drive authorization...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"Authorization complete. Token saved to: {TOKEN_FILE}")
    print("Drive uploads are now enabled.")

    # Quick connection test
    try:
        from googleapiclient.discovery import build
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        res = svc.files().list(pageSize=5, fields="files(id,name)").execute()
        files = res.get("files", [])
        print(f"Connection test: OK — can see {len(files)} item(s) in your Drive.")
    except Exception as e:
        print(f"Connection test failed: {e}")


if __name__ == "__main__":
    main()
