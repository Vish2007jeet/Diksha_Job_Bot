# Job Bot — Setup Guide

## Quick Start (5 Steps)

### 1. Install Python Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Your Profile — the ONLY file you need to edit

```bash
cp user_config.yaml.example user_config.yaml
# Open user_config.yaml and fill in ALL sections
```

`user_config.yaml` is the **single source of truth** for everything:

| Section | What to fill in |
|---|---|
| `api_keys` | Anthropic key, Telegram bot token + chat ID, LinkedIn cookie |
| `personal` | Your name, email, phone, LinkedIn handle, location |
| `profile.cv_profile` | Your full CV profile text (education, work exp, tools) — used in AI prompts |
| `profile.cl_profile` | Your cover letter candidate summary |
| `profile.interview_profile` | Your interview prep candidate context |
| `profile.cv_bullets` | All CV bullet points — used in interview prep STAR defence |
| `search` | Your job search keywords (tier1/tier2/tier3) and locations |
| `settings` | Scan interval, budget, scoring threshold |

> **.env is optional** — use it only for CI/Docker. `user_config.yaml` takes priority over `.env`.

### 3. Add Your CV and Cover Letter Templates

Place your DOCX files here:
```
templates/base/CV.docx
templates/base/CL.docx
```

**Mark sections for AI editing:** Highlight the text you want Claude to
customise in **YELLOW** (Home → Highlight in Word). Everything else stays
exactly as you wrote it.

Example:
- Professional Summary paragraph → highlight yellow
- Skills section content → highlight yellow  
- Job-specific experience bullet → highlight yellow

### 4. Keywords are managed live via Telegram

After first run, your tier1/tier2/tier3 keywords and locations are stored in
`data/keywords.json` and managed via Telegram:
```
/tier1   — view/edit Tier 1 keywords (direct match)
/tier2   — view/edit Tier 2 keywords (strong relevance)
/tier3   — view/edit Tier 3 keywords (background relevance)
/locations — view/edit preferred locations
```

The seed values from `user_config.yaml` are written to `keywords.json` on first run.

### 5. Start the Bot
```bash
python main.py
```

Then in Telegram: `/start` → `/scan`

---

## CV Template Colour Guide

| Colour | How to set in Word | Config value |
|--------|-------------------|--------------|
| Yellow (default) | Home → Highlight → Yellow | `YELLOW` |
| Cyan | Home → Highlight → Turquoise | `CYAN` |
| Red text | Format → Font → Red colour | `RED` |

Change in `user_config.yaml` → `settings.cv_highlight_color: YELLOW`

---

## Remote Triggering

The bot exposes a REST API on port 8000 (configurable).

```bash
# Trigger a full scan
curl -X POST "http://localhost:8000/scan?secret=YOUR_SECRET_KEY"

# Scan only Stepstone
curl -X POST "http://localhost:8000/scan/stepstone?secret=YOUR_SECRET_KEY"

# Get status
curl "http://localhost:8000/status?secret=YOUR_SECRET_KEY"

# List jobs
curl "http://localhost:8000/jobs?status=applied&secret=YOUR_SECRET_KEY"
```

### From n8n / Make (Integromat)
Use an HTTP Request node → POST → `http://your-server:8000/scan` with
`Authorization: Bearer YOUR_SECRET_KEY` header.

### From GitHub Actions
```yaml
- name: Trigger job scan
  run: |
    curl -X POST "${{ secrets.JOB_BOT_URL }}/scan" \
      -H "Authorization: Bearer ${{ secrets.JOB_BOT_SECRET }}"
```

---

## Telegram Commands

| Command | Action |
|---|---|
| `/start` | Show main menu |
| `/scan` | Trigger job scan now |
| `/jobs` | Show pending jobs to review |
| `/applications` | Application history |
| `/status` | Bot stats and config |
| `/keywords` | Show current keywords |
| `/help` | Help text |

### Job Card Buttons
- **✅ Apply** — Generates tailored CV + CL, logs application
- **❌ Skip** — Dismiss the job
- **🔖 Save** — Save for later review
- **📋 Full Description** — Show complete job text

---

## LinkedIn Scraping Note

LinkedIn actively blocks automated access. Options ranked best→worst:

1. **Cookie auth** (recommended): Log in via browser, copy the `li_at`
   cookie value from DevTools → Application → Cookies → `.linkedin.com`
   Paste it into `LINKEDIN_COOKIE` in `.env`

2. **Email/password**: Works but may trigger CAPTCHA. Use a separate
   account if possible.

3. **Skip LinkedIn**: Remove `linkedin` from scan sources in your Telegram
   scan trigger.

---

## File Structure

```
d:\Job_Bot\
├── main.py               ← Start here
├── orchestrator.py       ← Core scan workflow
├── config.py             ← Config loader
├── user_config.yaml      ← Your profile + keys (never commit! gitignored)
├── user_config.yaml.example  ← Template to copy
├── scrapers/             ← LinkedIn, Stepstone, Xing, Company
├── ai/                   ← Claude analysis + CV generation
├── bot/                  ← Telegram bot
├── documents/            ← DOCX processor + PDF exporter
├── tracking/             ← SQLite + Excel tracker
├── api/                  ← FastAPI remote trigger
├── data/
│   ├── jobs.db           ← SQLite database
│   ├── job_tracker.xlsx  ← Excel sheet (auto-updated)
│   └── applications/     ← Generated CV/CL files
└── templates/
    └── base/
        ├── CV.docx        ← YOUR BASE CV (add this!)
        └── CL.docx        ← YOUR BASE COVER LETTER (add this!)
```

---

## Excel Tracking Sheet

The file `data/job_tracker.xlsx` is automatically updated after each scan
and each application. Columns:

| Column | Description |
|---|---|
| Job ID | Unique hash |
| Source | linkedin / stepstone / xing |
| Title | Job title |
| Company | Company name |
| Location | City / Remote |
| Salary | If available |
| Score | AI relevance score (1–10) |
| Status | new → notified → applied → interviewing → offer |
| Applied At | Timestamp |
| URL | Clickable job link |
| CV/CL Path | Path to generated files |

Row colours indicate status at a glance.
