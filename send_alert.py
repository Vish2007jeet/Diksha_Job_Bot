"""
Emergency alert sender — run this manually on Windows if the bot is down
and Telegram alerts couldn't be sent by the health monitor.

Usage:
    cd D:\Job_Bot
    .venv\Scripts\python.exe send_alert.py
"""
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

# Load .env
env_path = Path(__file__).parent / ".env"
env_vars = {}
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip()

TOKEN = env_vars.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = env_vars.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    print("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not found in .env")
    sys.exit(1)

# Check for pending alert
alert_file = Path(__file__).parent / "data" / "pending_alert.json"
if alert_file.exists():
    alert = json.loads(alert_file.read_text(encoding="utf-8"))
    text = alert.get("message", "🚨 Job Bot alert (no message body)")
else:
    text = (
        "🚨 <b>Job Bot is DOWN!</b>\n\n"
        "Log file is empty — bot may not have started.\n\n"
        "▶️ Restart with:\n"
        "<code>cd D:\\Job_Bot &amp;&amp; .venv\\Scripts\\python.exe main.py</code>"
    )

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
resp = requests.post(url, data={
    "chat_id": CHAT_ID,
    "parse_mode": "HTML",
    "text": text,
})

result = resp.json()
if result.get("ok"):
    print("✅ Alert sent successfully!")
    # Mark as sent
    if alert_file.exists():
        alert["sent"] = True
        alert_file.write_text(json.dumps(alert, indent=2), encoding="utf-8")
else:
    print(f"❌ Failed to send alert: {result}")
    sys.exit(1)
