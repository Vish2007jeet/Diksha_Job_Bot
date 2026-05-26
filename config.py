"""
Central configuration — loaded from user_config.yaml (primary) with
.env / environment variables as fallback for CI/Docker deployments.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

# ── Load user_config.yaml ─────────────────────────────────────
_CONFIG_FILE = BASE_DIR / "user_config.yaml"
_cfg: dict = {}
if _CONFIG_FILE.exists():
    with _CONFIG_FILE.open(encoding="utf-8") as _f:
        _cfg = yaml.safe_load(_f) or {}

def _yaml(section: str, key: str, env_var: str = "", default=None):
    """Return value: user_config.yaml > env var > default."""
    val = _cfg.get(section, {}).get(key)
    if val is not None and str(val).strip() != "":
        return val
    if env_var:
        env_val = os.getenv(env_var)
        if env_val is not None and env_val.strip() != "":
            return env_val
    return default

# ── Anthropic ────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = _yaml("api_keys", "anthropic_api_key", "ANTHROPIC_API_KEY") or os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL: str = _yaml("settings", "claude_model", "CLAUDE_MODEL", "claude-sonnet-4-6")
MIN_QUALITY_SCORE: int = int(_yaml("settings", "min_quality_score", "MIN_QUALITY_SCORE", 90))
SCAN_TIMEOUT_HOURS: int = int(_yaml("settings", "scan_timeout_hours", "SCAN_TIMEOUT_HOURS", 2))
JD_FETCH_CONCURRENCY: int = int(_yaml("settings", "jd_fetch_concurrency", "JD_FETCH_CONCURRENCY", 6))

# ── Telegram ─────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = _yaml("api_keys", "telegram_bot_token", "TELEGRAM_BOT_TOKEN") or os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID: int = int(_yaml("api_keys", "telegram_chat_id", "TELEGRAM_CHAT_ID") or os.environ["TELEGRAM_CHAT_ID"])

# ── Credentials ──────────────────────────────────────────────
LINKEDIN_EMAIL: str    = _yaml("api_keys", "linkedin_email",    "LINKEDIN_EMAIL",    "")
LINKEDIN_PASSWORD: str = _yaml("api_keys", "linkedin_password", "LINKEDIN_PASSWORD", "")
LINKEDIN_COOKIE: str   = _yaml("api_keys", "linkedin_cookie",   "LINKEDIN_COOKIE",   "")
XING_EMAIL: str        = _yaml("api_keys", "xing_email",        "XING_EMAIL",        "")
XING_PASSWORD: str     = _yaml("api_keys", "xing_password",     "XING_PASSWORD",     "")

# ── Personal Profile ──────────────────────────────────────────
_personal = _cfg.get("personal", {})
USER_FULL_NAME: str  = _personal.get("full_name")      or os.getenv("USER_FULL_NAME",  "Your Name")
USER_NAME_SHORT: str = _personal.get("name_short")     or os.getenv("USER_NAME_SHORT", USER_FULL_NAME)
USER_EMAIL: str      = _personal.get("email")          or os.getenv("USER_EMAIL",      "")
USER_PHONE: str      = _personal.get("phone")          or os.getenv("USER_PHONE",      "")
USER_LINKEDIN: str   = _personal.get("linkedin_handle") or os.getenv("USER_LINKEDIN",  "")
USER_LOCATION: str   = _personal.get("location")       or os.getenv("USER_LOCATION",   "")

# ── AI Profile Texts ──────────────────────────────────────────
_profile = _cfg.get("profile", {})
CV_PROFILE_TEXT: str        = _profile.get("cv_profile")        or os.getenv("CV_PROFILE_TEXT",        "")
CL_PROFILE_TEXT: str        = _profile.get("cl_profile")        or os.getenv("CL_PROFILE_TEXT",        "")
INTERVIEW_PROFILE_TEXT: str = _profile.get("interview_profile") or os.getenv("INTERVIEW_PROFILE_TEXT", "")
CV_BULLETS_TEXT: str        = _profile.get("cv_bullets")        or os.getenv("CV_BULLETS_TEXT",        "")
CV_STAR_SECTION_LABELS: List[str] = _profile.get("cv_star_section_labels") or [
    "7.1 Role 1 Experience",
    "7.2 Role 2 Experience",
    "7.3 Role 3 Experience",
    "7.4 Role 4 Experience",
    "7.5 Extracurricular / Projects",
]

# ── Search Parameters ─────────────────────────────────────────
def _split(val: str) -> List[str]:
    return [v.strip() for v in val.split(",") if v.strip()]

_search = _cfg.get("search", {})

SEARCH_KEYWORDS: List[str] = (
    _search.get("tier1_keywords")
    or _split(os.getenv("SEARCH_KEYWORDS", "Werkstudent,Working Student,Internship"))
)
SEARCH_LOCATIONS: List[str] = (
    _search.get("locations")
    or _split(os.getenv("SEARCH_LOCATIONS", "Germany,Remote"))
)
SEARCH_LANGUAGE: str = _search.get("language") or os.getenv("SEARCH_LANGUAGE", "en")

# NOTE: Active keyword and location lists are managed live via keywords.json
# (edit via Telegram /keywords and /locations). SEARCH_KEYWORDS above is only
# used as the initial seed for the "exact" list when keywords.json doesn't exist.

# ── Keyword Taxonomy — first-run seeds for keywords.json ─────────
# Active values are managed live in data/keywords.json via /tier1 /tier2 /tier3.
# These lists only apply when keywords.json does not yet exist.
# If user_config.yaml has search.tier1_keywords, those are used instead.
TIER1_KEYWORDS: List[str] = _search.get("tier1_keywords") or [
    # Vehicle Dynamics & Chassis
    "Vehicle Dynamics", "Fahrzeugdynamik", "Multi Body Dynamics", "MBD", "Adams",
    "Suspension", "Fahrwerk", "Chassis Development", "Fahrwerksentwicklung",
    "Anti-roll Bar", "Stabilisator", "Tyre Load Sensitivity", "NVH",
    # EV & Powertrain
    "EV Powertrain", "Elektroantrieb", "Battery Systems", "Batteriesystem",
    "HV Battery", "Hochvoltbatterie", "PHEV", "E-Mobility", "Elektromobilität",
    "Electric Motor", "Elektromotor", "Drivetrain", "Antriebsstrang",
    # Brake & Steer by Wire
    "Brake-by-Wire", "BBW", "Steer-by-Wire", "SBW", "Brake Systems", "Bremssystem",
    # Job type
    "Werkstudent", "Working Student", "Masterarbeit", "Master Thesis",
    "Praktikum", "Internship", "Abschlussarbeit",
]

TIER2_KEYWORDS: List[str] = _search.get("tier2_keywords") or [
    # Simulation tools
    "MATLAB", "Simulink", "ANSYS", "Ansys Workbench", "FEA", "OptiSLang",
    "CATIA", "CATIA V5", "SolidWorks", "CarMaker", "CarSim", "dSPACE", "HIL",
    # Domain
    "Thermal Management", "Thermomanagement", "NVH", "Actuator", "Aktorik",
    "Power Electronics", "Leistungselektronik", "BMS", "Battery Management",
    # General engineering
    "Python", "Power BI", "CAE", "CFD", "R&D", "Forschung",
    "Vehicle Testing", "Erprobung", "Prototype", "Prototyp",
    "Formula Student", "Motorsport", "Powertrain Engineering",
]

TIER3_KEYWORDS: List[str] = _search.get("tier3_keywords") or [
    "Power Electronics", "Leistungselektronik", "Electric Motor", "E-Maschine",
    "BMS", "Battery Management", "Prototype Development", "Series Development",
    "OEM", "Automotive", "Fahrzeugentwicklung",
    "SQL", "Excel VBA", "SAP", "Tableau", "UG-NX", "Inventor",
    "SolidWorks Flow Simulation",
]

_settings = _cfg.get("settings", {})

# ── Scraper Feature Flags ──────────────────────────────────────
# Indeed: permanently blocked on German IPs + free proxy pools don't help.
# Coverage replaced by Stepstone + Xing + Arbeitsagentur.
# Set INDEED_ENABLED=true in .env only if you have a working residential proxy.
INDEED_ENABLED: bool = os.getenv("INDEED_ENABLED", "false").lower() == "true"

# ── Filtering ─────────────────────────────────────────────────
MIN_RELEVANCE_SCORE: float = float(_settings.get("min_relevance_score") or os.getenv("MIN_RELEVANCE_SCORE", "6"))
API_MONTHLY_BUDGET: float  = float(os.getenv("API_MONTHLY_BUDGET", "0") or "0")
MAX_JOBS_PER_SCAN: int     = int(_settings.get("max_jobs_per_scan") or os.getenv("MAX_JOBS_PER_SCAN", "20"))

# ── Expense Budget ────────────────────────────────────────────
# Monthly spend limit in EUR shown in /expense command.
# USD costs are converted using a fixed approximate rate.
MONTHLY_BUDGET_EUR: float = float(_settings.get("monthly_budget_eur") or os.getenv("MONTHLY_BUDGET_EUR", "50.0"))
EUR_TO_USD_RATE:    float = float(_settings.get("eur_to_usd_rate")    or os.getenv("EUR_TO_USD_RATE",    "1.10"))

# ── Scheduling ────────────────────────────────────────────────
SCAN_INTERVAL_HOURS: int = int(_settings.get("scan_interval_hours") or os.getenv("SCAN_INTERVAL_HOURS", "4"))

# ── API Server ────────────────────────────────────────────────
API_HOST: str       = _settings.get("api_host") or os.getenv("API_HOST", "0.0.0.0")
API_PORT: int       = int(_settings.get("api_port") or os.getenv("API_PORT", "8000"))
API_SECRET_KEY: str = _yaml("api_keys", "api_secret_key", "API_SECRET_KEY", "change-me")

# ── File Paths ────────────────────────────────────────────────
CV_TEMPLATE_PATH: Path = BASE_DIR / os.getenv("CV_TEMPLATE_PATH", "templates/base/CV.docx")
CL_TEMPLATE_PATH: Path = BASE_DIR / os.getenv("CL_TEMPLATE_PATH", "templates/base/CL.docx")
OUTPUT_DIR: Path = BASE_DIR / os.getenv("OUTPUT_DIR", "data/applications")
TRACKING_EXCEL: Path = BASE_DIR / os.getenv("TRACKING_EXCEL", "data/job_tracker.xlsx")
DATABASE_PATH: Path = BASE_DIR / "data" / "jobs.db"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── CV Color Config ───────────────────────────────────────────
CV_HIGHLIGHT_COLOR: str = _settings.get("cv_highlight_color") or os.getenv("CV_HIGHLIGHT_COLOR", "YELLOW")

# ── Runtime toggles (persisted in data/bot_settings.json, managed via Telegram) ──
# Loaded here so the rest of the codebase can do `import config; config.HUMANIZE_ENABLED`.
# utils/bot_settings.py handles reads/writes and keeps this in sync when changed.
def _load_bot_setting(key: str, default):
    _f = BASE_DIR / "data" / "bot_settings.json"
    if _f.exists():
        try:
            import json as _json
            return _json.loads(_f.read_text(encoding="utf-8")).get(key, default)
        except Exception:
            pass
    return default

HUMANIZE_ENABLED: bool = bool(_load_bot_setting("humanize_enabled", True))
ATS_SCORE_TARGET: int  = int(_load_bot_setting("ats_score_target", 80))
CV_BEST_OF_N: int      = max(1, int(_load_bot_setting("cv_best_of_n", 1)))
CL_BEST_OF_N: int      = max(1, int(_load_bot_setting("cl_best_of_n", 1)))

# ── Google Sheets ─────────────────────────────────────────────
_google = _cfg.get("google", {})
GOOGLE_SHEETS_ID: str = _google.get("sheets_id") or os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_CREDENTIALS_PATH: Path = BASE_DIR / (
    _google.get("credentials_path")
    or os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/google_service_account.json")
)

# ── Google Drive ──────────────────────────────────────────────
# ID of the root Drive folder to upload CV/CL folders into.
# Get from URL: drive.google.com/drive/folders/<THIS_ID>
GOOGLE_DRIVE_FOLDER_ID: str = _google.get("drive_folder_id") or os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
GWS_EXE: Path = BASE_DIR / "tools" / "gws.exe"   # googleworkspace/cli binary

# ── Workday Career Sites ──────────────────────────────────────
# All previous entries were automotive Tier-1s/OEMs (Magna, Valeo, BorgWarner,
# Aptiv, Continental, ZF, Schaeffler, Infineon, Harman, Tesla, NIO, Bosch,
# Vitesco, Forvia HELLA, Mahle, ElringKlinger, TE Connectivity, Brose, Leoni,
# Bosch Rexroth, Rheinmetall). Cleared 2026-05-26 — user's domain is
# BA/BI/Controlling/Data Werkstudent, not automotive engineering. Workday is
# not a relevant discovery channel for this domain.
WORKDAY_SITES: List[dict] = []

# ── Personio Career Sites ─────────────────────────────────────
# STATUS 2026-05-05: Personio deprecated the *.jobs.personio.{de,com} wildcard
# subdomain API. ALL subdomains now 307-redirect to personio.com homepage.
# Companies that were here have been moved to TARGET_COMPANIES for LinkedIn coverage.
# Former tenants and their new ATS:
#   EDAG Engineering     → LinkedIn (own portal, no public API)
#   Knorr-Bremse         → SAP SuccessFactors (careers.knorr-bremse.com)
#   MAN Truck & Bus      → SAP SuccessFactors (jobs.man.eu)
#   Bertrandt            → SAP SuccessFactors (bertrandt.jobs.hr.cloud.sap)
#   IAV GmbH             → SAP SuccessFactors (career5.successfactors.eu/?company=iavgmbh)
#   FEV Group            → Own portal (career.fev.com)
#   AVL Deutschland      → LinkedIn (own portal)
#   Dürr AG              → LinkedIn (own portal)
#   Haldex               → LinkedIn (own portal)
#   Expleo Germany       → Own portal (careers.expleo.com)
#   Segula Technologies  → SmartRecruiters (careers.segulatechnologies.com)
#   ALTEN GmbH           → LinkedIn (alten.com)
PERSONIO_SITES: List[dict] = []


# ── Company Websites — generic HTML scraper ───────────────────
# All previous entries were EV OEMs (BYD, Xiaomi, CATL). Cleared 2026-05-26
# — user's domain is BA/BI/Controlling/Data Werkstudent, not EV/automotive.
COMPANY_SITES: List[dict] = []

# ── Target Companies — dedicated LinkedIn scrape targets ───────
# Consumed by scrapers/target_companies.py (TargetCompanyScraper).
# Searches LinkedIn specifically for these EV / tech OEMs in Germany
# regardless of whether their job titles match the keyword taxonomy.
# The AI scorer handles relevance filtering afterwards.
#
# Fields:
#   name          : canonical name shown in Telegram cards
#   name_variants : aliases accepted in LinkedIn "company" field
#                   (fuzzy substring — "Tesla GmbH" matches variant "Tesla")
#   search_terms  : paired with company name → LinkedIn query string
#                   "{company_name} {term}" searched in Germany
TARGET_COMPANIES: List[dict] = []

# ═══════════════════════════════════════════════════════════════════
# AUTOMOTIVE EXPANSION — added 2026-04-28
# Covers major OEMs, Tier-1/2 suppliers, EV specialists, simulation
# houses, and engineering service providers active in Germany.
# ═══════════════════════════════════════════════════════════════════

# All WORKDAY_SITES.extend automotive blocks removed 2026-05-26.

# PERSONIO_SITES.extend([]) — all former entries cleared 2026-05-05 (see note above)

# ── LinkedIn target companies — Data + Business Werkstudent ──────
# Replaced automotive list 2026-05-26. Focus: BA/BI/Controlling/Data/PM
# Werkstudent at mid-size Bavarian companies with better odds than top-tier OEMs.
TARGET_COMPANIES.extend([
    # ── Tier A — Werkstudent BA/BI/Data factories (Munich/Bavaria) ──
    {
        "name": "Check24",
        "name_variants": ["Check24", "CHECK24", "Check24 Vergleichsportal", "Check24 GmbH"],
        "search_terms": [
            "Werkstudent Business Analytics",
            "Werkstudent Data",
            "Werkstudent Controlling",
        ],
    },
    {
        "name": "Celonis",
        "name_variants": ["Celonis", "Celonis SE", "Celonis GmbH"],
        "search_terms": [
            "Working Student Process Mining",
            "Working Student Data Analyst",
            "Werkstudent Customer Analytics",
        ],
    },
    {
        "name": "Personio",
        "name_variants": ["Personio", "Personio SE", "Personio GmbH"],
        "search_terms": [
            "Werkstudent People Analytics",
            "Werkstudent Business Intelligence",
            "Working Student Data",
        ],
    },
    {
        "name": "DATEV",
        "name_variants": ["DATEV", "DATEV eG", "DATEV Software", "DATEV Magazin"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Business Intelligence",
            "Werkstudent Reporting",
        ],
    },
    {
        "name": "msg systems",
        "name_variants": ["msg systems", "msg", "msg systems ag", "msg group", "msg systems AG"],
        "search_terms": [
            "Werkstudent Business Intelligence",
            "Werkstudent Data Analytics",
            "Werkstudent SAP",
        ],
    },
    {
        "name": "adesso",
        "name_variants": ["adesso", "adesso SE", "adesso AG", "adesso group"],
        "search_terms": [
            "Werkstudent Business Intelligence",
            "Werkstudent Data Analytics",
            "Werkstudent Power BI",
        ],
    },
    {
        "name": "TNG Technology Consulting",
        "name_variants": ["TNG", "TNG Technology Consulting", "TNG Technology Consulting GmbH"],
        "search_terms": [
            "Werkstudent Data",
            "Werkstudent Analytics",
            "Working Student Data Science",
        ],
    },
    {
        "name": "Sopra Steria",
        "name_variants": ["Sopra Steria", "Sopra Steria SE", "Sopra Steria Consulting"],
        "search_terms": [
            "Werkstudent Data Analytics",
            "Werkstudent Business Intelligence",
            "Werkstudent Controlling",
        ],
    },
    {
        "name": "Capco",
        "name_variants": ["Capco", "Capco Germany", "The Capital Markets Company"],
        "search_terms": [
            "Werkstudent Data",
            "Werkstudent Business Analyst",
            "Working Student Consulting",
        ],
    },
    {
        "name": "Cofinpro",
        "name_variants": ["Cofinpro", "Cofinpro AG", "Cofinpro GmbH"],
        "search_terms": [
            "Werkstudent Banking",
            "Werkstudent Business Analyst",
            "Werkstudent Reporting",
        ],
    },
    # ── Tier B — Insurance / banking back-office (controlling + BI) ──
    {
        "name": "Allianz Technology",
        "name_variants": ["Allianz Technology", "Allianz Technology SE", "AZ Technology"],
        "search_terms": [
            "Werkstudent Data Analytics",
            "Werkstudent Business Intelligence",
            "Werkstudent Controlling",
        ],
    },
    {
        "name": "Versicherungskammer Bayern",
        "name_variants": ["Versicherungskammer Bayern", "Versicherungskammer", "VKB"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Datenanalyse",
            "Werkstudent Reporting",
        ],
    },
    {
        "name": "UniCredit",
        "name_variants": ["UniCredit", "HypoVereinsbank", "HVB", "UniCredit Bank GmbH", "UniCredit Bank AG"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Business Intelligence",
            "Werkstudent Risk",
        ],
    },
    {
        "name": "BayernLB",
        "name_variants": ["BayernLB", "Bayerische Landesbank", "Bayern LB"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Reporting",
            "Werkstudent Data",
        ],
    },
    {
        "name": "Generali Deutschland",
        "name_variants": ["Generali", "Generali Deutschland", "Generali Deutschland AG", "Generali Versicherung"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Business Intelligence",
            "Werkstudent Datenanalyse",
        ],
    },
    {
        "name": "Stadtsparkasse München",
        "name_variants": ["Stadtsparkasse München", "Stadtsparkasse Muenchen", "SSKM"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Datenanalyse",
            "Werkstudent Reporting",
        ],
    },
    # ── Tier C — Munich tech / digital with strong data teams ──────
    {
        "name": "Scout24",
        "name_variants": ["Scout24", "Scout24 SE", "ImmoScout24", "AutoScout24"],
        "search_terms": [
            "Werkstudent Data Analytics",
            "Werkstudent Business Intelligence",
            "Working Student Data",
        ],
    },
    {
        "name": "ProSiebenSat.1",
        "name_variants": ["ProSiebenSat.1", "ProSiebenSat.1 Media", "ProSieben", "P7S1"],
        "search_terms": [
            "Werkstudent Data Analytics",
            "Werkstudent Business Intelligence",
            "Werkstudent Controlling",
        ],
    },
    {
        "name": "Westwing",
        "name_variants": ["Westwing", "Westwing Group", "Westwing Home & Living"],
        "search_terms": [
            "Werkstudent Data Analytics",
            "Werkstudent Business Intelligence",
            "Werkstudent Controlling",
        ],
    },
    {
        "name": "Holidu",
        "name_variants": ["Holidu", "Holidu GmbH", "Holidu Travel"],
        "search_terms": [
            "Werkstudent Data Analytics",
            "Working Student Business Intelligence",
            "Werkstudent Marketing Analytics",
        ],
    },
    {
        "name": "Stylight",
        "name_variants": ["Stylight", "Stylight GmbH", "ProSiebenSat.1 Commerce"],
        "search_terms": [
            "Werkstudent Data",
            "Working Student Analytics",
            "Werkstudent Marketing Analytics",
        ],
    },
    {
        "name": "Sportradar",
        "name_variants": ["Sportradar", "Sportradar AG", "Sportradar Group"],
        "search_terms": [
            "Working Student Data Analyst",
            "Werkstudent Data",
            "Werkstudent Business Intelligence",
        ],
    },
    {
        "name": "IDnow",
        "name_variants": ["IDnow", "IDnow GmbH", "ID now"],
        "search_terms": [
            "Werkstudent Data Analytics",
            "Werkstudent Business Analyst",
            "Werkstudent Reporting",
        ],
    },
    # ── Tier D — Industrial Munich mid-caps (sleepy careers pages, controlling/BI) ──
    {
        "name": "Stadtwerke München",
        "name_variants": ["Stadtwerke München", "Stadtwerke Muenchen", "SWM", "SWM München"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Business Intelligence",
            "Werkstudent Datenanalyse",
        ],
    },
    {
        "name": "Wacker Chemie",
        "name_variants": ["Wacker", "Wacker Chemie", "Wacker Chemie AG", "WACKER"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent SAP",
            "Werkstudent Business Intelligence",
        ],
    },
    {
        "name": "Linde",
        "name_variants": ["Linde", "Linde plc", "Linde AG", "Linde Engineering"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Data Analytics",
            "Werkstudent Reporting",
        ],
    },
    {
        "name": "Rohde & Schwarz",
        "name_variants": ["Rohde & Schwarz", "Rohde und Schwarz", "Rohde&Schwarz", "R&S"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Business Intelligence",
            "Werkstudent Data Analytics",
        ],
    },
    {
        "name": "Giesecke+Devrient",
        "name_variants": ["Giesecke+Devrient", "Giesecke & Devrient", "G+D", "Giesecke Devrient"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent Business Intelligence",
            "Werkstudent Data",
        ],
    },
    {
        "name": "KraussMaffei",
        "name_variants": ["KraussMaffei", "Krauss Maffei", "KraussMaffei Group", "KraussMaffei Technologies"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent SAP",
            "Werkstudent Data Analytics",
        ],
    },
    {
        "name": "Krones",
        "name_variants": ["Krones", "Krones AG", "Krones Group"],
        "search_terms": [
            "Werkstudent Controlling",
            "Werkstudent SAP",
            "Werkstudent Business Intelligence",
        ],
    },
])
