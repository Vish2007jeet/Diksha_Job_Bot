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
# api_url  : POST target for Workday job search API
# career_url: base URL prepended to externalPath for job links
WORKDAY_SITES: List[dict] = [
    # myworkdaysite.com — verified working
    {
        "name": "Magna International",
        "api_url": "https://wd3.myworkdaysite.com/wday/cxs/magna/Magna/jobs",
        "career_url": "https://wd3.myworkdaysite.com/recruiting/magna/Magna",
        "location": "Germany",
    },
    # myworkdayjobs.com — verified working
    {
        "name": "Valeo",
        "api_url": "https://valeo.wd3.myworkdayjobs.com/wday/cxs/valeo/valeo_jobs/jobs",
        "career_url": "https://valeo.wd3.myworkdayjobs.com/en-EN/valeo_jobs",
        "location": "Germany",
    },
    {
        "name": "BorgWarner",
        "api_url": "https://borgwarner.wd5.myworkdayjobs.com/wday/cxs/borgwarner/BorgWarner_Careers/jobs",
        "career_url": "https://borgwarner.wd5.myworkdayjobs.com/BorgWarner_Careers",
        "location": "Germany",
    },
    {
        "name": "Aptiv",
        "api_url": "https://aptiv.wd5.myworkdayjobs.com/wday/cxs/aptiv/APTIV_CAREERS/jobs",
        "career_url": "https://aptiv.wd5.myworkdayjobs.com/APTIV_CAREERS",
        "location": "Germany",
    },
    # unverified — will return [] with a warning if URL is wrong
    {
        "name": "Continental",
        "api_url": "https://continental.wd3.myworkdayjobs.com/wday/cxs/continental/Jobs/jobs",
        "career_url": "https://continental.wd3.myworkdayjobs.com/Jobs",
        "location": "Germany",
    },
    {
        "name": "ZF Friedrichshafen",
        "api_url": "https://zf.wd3.myworkdayjobs.com/wday/cxs/zf/ZF_Careers/jobs",
        "career_url": "https://zf.wd3.myworkdayjobs.com/ZF_Careers",
        "location": "Germany",
    },
    {
        "name": "Schaeffler",
        "api_url": "https://schaeffler.wd3.myworkdayjobs.com/wday/cxs/schaeffler/Schaeffler_Careers/jobs",
        "career_url": "https://schaeffler.wd3.myworkdayjobs.com/Schaeffler_Careers",
        "location": "Germany",
    },
    {
        "name": "Infineon",
        "api_url": "https://infineon.wd3.myworkdayjobs.com/wday/cxs/infineon/InfineonCareers/jobs",
        "career_url": "https://infineon.wd3.myworkdayjobs.com/InfineonCareers",
        "location": "Germany",
    },
    {
        "name": "Harman",
        "api_url": "https://harman.wd1.myworkdayjobs.com/wday/cxs/harman/HarmanCareers/jobs",
        "career_url": "https://harman.wd1.myworkdayjobs.com/HarmanCareers",
        "location": "Germany",
    },
]

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


# ── Company Websites — EV / new-energy OEM career portals ─────
# Uses the generic HTML scraper (CompanyScraper).
# NOTE: JS-rendered sites return 0 results — LinkedIn TargetCompanyScraper
#       is the primary discovery path for these companies.
# Format: name, url, job_selector, title_selector, link_selector,
#         location_selector, location
COMPANY_SITES: List[dict] = [
    # BYD Europe — Frankfurt / Nuremberg offices
    # Static HTML renders job cards in .jobs-list-container li (no JS needed).
    # Verified 2026-05-03: 20 job cards returned from static HTML.
    {
        "name": "BYD Auto",
        "url": "https://careers.bydeurope.com/jobs",
        "job_selector": ".jobs-list-container li",
        "title_selector": "a, h2, h3, .job-title",
        "link_selector": "a",
        "location_selector": ".text-base",
        "location": "Germany",
        "js_rendered": False,
    },
    # Xiaomi — European HQ Düsseldorf + Munich
    # JS-rendered SPA — covered by TargetCompanyScraper (LinkedIn).
    # Kept here so the URL stays current; errors suppressed to DEBUG.
    {
        "name": "Xiaomi",
        "url": "https://career.mi.com/home",
        "job_selector": ".job-item, .career-item, li[data-ph-at-id]",
        "title_selector": "h2, h3, .job-title",
        "link_selector": "a",
        "location_selector": ".location, .job-location",
        "location": "Germany",
        "js_rendered": True,
    },
    # CATL — Erfurt gigafactory + Munich engineering hub
    # Official career site: catl-career.com (NOT catl.com/en/join — that's the corporate site)
    # JS-rendered SPA — BeautifulSoup returns 0 cards; covered by TargetCompanyScraper (LinkedIn).
    {
        "name": "CATL",
        "url": "https://www.catl-career.com/viewalljobs/",
        "job_selector": ".job-card, .position-item, article.job, li.opening",
        "title_selector": "h2, h3, .job-title, .position-title",
        "link_selector": "a",
        "location_selector": ".location, .position-location, .job-location",
        "location": "Germany",
        "js_rendered": True,
    },
]

# Extend WORKDAY_SITES with EV OEMs that use Workday ATS
WORKDAY_SITES.extend([
    # Tesla — verified Workday tenant (Gigafactory Berlin + Munich offices)
    {
        "name": "Tesla",
        "api_url": "https://tesla.wd1.myworkdayjobs.com/wday/cxs/tesla/TeslaMotors/jobs",
        "career_url": "https://tesla.wd1.myworkdayjobs.com/TeslaMotors",
        "location": "Germany",
    },
    # NIO — Munich R&D hub; verified wd3/NIO_Careers tenant
    {
        "name": "NIO",
        "api_url": "https://nio.wd3.myworkdayjobs.com/wday/cxs/nio/NIO_Careers/jobs",
        "career_url": "https://nio.wd3.myworkdayjobs.com/NIO_Careers",
        "location": "Germany",
    },
    # XPENG — uses Greenhouse ATS (job-boards.greenhouse.io/xpengmotors), NOT Workday.
    # Covered by TargetCompanyScraper via LinkedIn. Disabled here to avoid silent 404s.
    # {
    #     "name": "XPENG",
    #     "api_url": "https://xpeng.wd3.myworkdayjobs.com/wday/cxs/xpeng/XPENG/jobs",
    #     "career_url": "https://xpeng.wd3.myworkdayjobs.com/XPENG",
    #     "location": "Germany",
    # },
])

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
TARGET_COMPANIES: List[dict] = [
    {
        "name": "Tesla",
        "name_variants": ["Tesla", "Tesla Motors", "Tesla Deutschland", "Tesla Germany"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum engineering",
            "working student automotive",
            "intern engineer",
            "Masterarbeit",
        ],
    },
    {
        "name": "BYD",
        "name_variants": ["BYD", "BYD Auto", "BYD Europe", "BYD Auto GmbH", "BYD Company"],
        "search_terms": [
            "Werkstudent engineering",
            "Praktikum automotive",
            "intern engineer Germany",
            "Ingenieur",
        ],
    },
    {
        "name": "Xiaomi",
        "name_variants": ["Xiaomi", "Xiaomi Deutschland", "Xiaomi Germany", "Xiaomi GmbH"],
        "search_terms": [
            "engineer Germany",
            "Ingenieur",
            "Werkstudent",
            "intern",
        ],
    },
    {
        "name": "NIO",
        "name_variants": ["NIO", "NIO GmbH", "NIO Germany", "NIO Europe"],
        "search_terms": [
            "Werkstudent automotive",
            "engineer Munich",
            "Praktikum",
            "working student",
        ],
    },
    {
        "name": "CATL",
        "name_variants": ["CATL", "Contemporary Amperex", "CATL Germany", "CATL Europe"],
        "search_terms": [
            "battery engineer",
            "Batterieingenieur",
            "Werkstudent",
            "Praktikum Germany",
        ],
    },
    {
        "name": "Polestar",
        "name_variants": ["Polestar", "Polestar Automotive", "Polestar Germany"],
        "search_terms": [
            "engineer Germany",
            "Werkstudent automotive",
            "Praktikum",
        ],
    },
    {
        "name": "Rivian",
        "name_variants": ["Rivian", "Rivian Automotive"],
        "search_terms": [
            "engineer Germany",
            "Ingenieur",
        ],
    },
]

# ═══════════════════════════════════════════════════════════════════
# AUTOMOTIVE EXPANSION — added 2026-04-28
# Covers major OEMs, Tier-1/2 suppliers, EV specialists, simulation
# houses, and engineering service providers active in Germany.
# ═══════════════════════════════════════════════════════════════════

# ── Additional Workday ATS companies ──────────────────────────
# All marked unverified unless noted; WorkdayScraper returns []
# gracefully on 4xx so wrong URLs cause no harm.
WORKDAY_SITES.extend([
    # ── Tier-1 Suppliers ──────────────────────────────────────
    {
        "name": "Robert Bosch",
        "api_url": "https://bosch.wd3.myworkdayjobs.com/wday/cxs/bosch/BoschExternalCareerSite/jobs",
        "career_url": "https://bosch.wd3.myworkdayjobs.com/BoschExternalCareerSite",
        "location": "Germany",
    },
    # Vitesco was acquired by Schaeffler (2024). Their career portal at
    # jobs.vitesco-technologies.com redirects to Schaeffler jobs.
    # Workday URL unverified — returns [] silently on 404; covered by LinkedIn.
    {
        "name": "Vitesco Technologies",
        "api_url": "https://vitesco-technologies.wd3.myworkdayjobs.com/wday/cxs/vitesco-technologies/VitescoTechnologies/jobs",
        "career_url": "https://vitesco-technologies.wd3.myworkdayjobs.com/VitescoTechnologies",
        "location": "Germany",
    },
    # Forvia HELLA — uses hella.com/en/Career portal (SAP SF backend likely).
    # Workday URL unverified; returns [] silently on 404.
    {
        "name": "Forvia HELLA",
        "api_url": "https://forvia.wd3.myworkdayjobs.com/wday/cxs/forvia/HELLA/jobs",
        "career_url": "https://forvia.wd3.myworkdayjobs.com/HELLA",
        "location": "Germany",
    },
    # MAHLE — uses careers.mahle.com / jobs.mahle.com (own portal).
    # Workday URL unverified; returns [] silently on 404.
    {
        "name": "Mahle",
        "api_url": "https://mahle.wd3.myworkdayjobs.com/wday/cxs/mahle/MahleGroup/jobs",
        "career_url": "https://mahle.wd3.myworkdayjobs.com/MahleGroup",
        "location": "Germany",
    },
    {
        "name": "ElringKlinger",
        # Verified: public job URLs use /External/ — tenant name is "External"
        "api_url": "https://elringklinger.wd3.myworkdayjobs.com/wday/cxs/elringklinger/External/jobs",
        "career_url": "https://elringklinger.wd3.myworkdayjobs.com/External",
        "location": "Germany",
    },
    {
        "name": "TE Connectivity",
        "api_url": "https://te.wd1.myworkdayjobs.com/wday/cxs/te/TEConnectivityJobs/jobs",
        "career_url": "https://te.wd1.myworkdayjobs.com/TEConnectivityJobs",
        "location": "Germany",
    },
    {
        "name": "Brose",
        "api_url": "https://brose.wd3.myworkdayjobs.com/wday/cxs/brose/Brose/jobs",
        "career_url": "https://brose.wd3.myworkdayjobs.com/Brose",
        "location": "Germany",
    },
    {
        "name": "Leoni",
        "api_url": "https://leoni.wd3.myworkdayjobs.com/wday/cxs/leoni/Leoni/jobs",
        "career_url": "https://leoni.wd3.myworkdayjobs.com/Leoni",
        "location": "Germany",
    },
    # ── Tech / Electronics ────────────────────────────────────
    # Siemens AG uses jobs.siemens.com (SAP SuccessFactors), NOT Workday.
    # siemensgamesa.wd3.myworkdayjobs.com is Siemens Gamesa (wind energy, unrelated).
    # Siemens is covered by TARGET_COMPANIES (LinkedIn). Disabled here.
    # {
    #     "name": "Siemens",
    #     "api_url": "https://siemens.wd3.myworkdayjobs.com/wday/cxs/siemens/Siemens/jobs",
    #     "career_url": "https://siemens.wd3.myworkdayjobs.com/Siemens",
    #     "location": "Germany",
    # },
    {
        "name": "Bosch Rexroth",
        "api_url": "https://boschrexroth.wd3.myworkdayjobs.com/wday/cxs/boschrexroth/BoschRexroth/jobs",
        "career_url": "https://boschrexroth.wd3.myworkdayjobs.com/BoschRexroth",
        "location": "Germany",
    },
    # ── Defense / Industrial Automotive ──────────────────────
    # Rheinmetall uses rheinmetall.com/en/career/vacancies (SAP or custom ATS).
    # Workday URL unverified; returns [] silently on 404.
    {
        "name": "Rheinmetall",
        "api_url": "https://rheinmetall.wd3.myworkdayjobs.com/wday/cxs/rheinmetall/Rheinmetall/jobs",
        "career_url": "https://rheinmetall.wd3.myworkdayjobs.com/Rheinmetall",
        "location": "Germany",
    },
])

# PERSONIO_SITES.extend([]) — all former entries cleared 2026-05-05 (see note above)

# ── Additional LinkedIn target companies ──────────────────────
# Major OEMs + Tier-1 suppliers + simulation houses not on Workday/Personio
TARGET_COMPANIES.extend([
    # ── German OEMs ──────────────────────────────────────────
    {
        "name": "BMW Group",
        "name_variants": ["BMW", "BMW Group", "BMW AG", "BMW M GmbH", "BMW Motorrad"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Fahrzeugtechnik",
            "working student automotive",
            "Masterarbeit Fahrzeug",
            "intern engineer Munich",
        ],
    },
    {
        "name": "Mercedes-Benz",
        "name_variants": ["Mercedes-Benz", "Mercedes Benz", "Mercedes-Benz AG", "Mercedes-Benz Group"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum engineering",
            "working student automotive",
            "Masterarbeit",
            "intern engineer Stuttgart",
        ],
    },
    {
        "name": "Volkswagen",
        "name_variants": ["Volkswagen", "Volkswagen AG", "VW AG", "Volkswagen Group", "Volkswagen Pkw"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Fahrzeugentwicklung",
            "working student engineering",
            "Masterarbeit Fahrzeugtechnik",
        ],
    },
    {
        "name": "Audi",
        "name_variants": ["Audi", "Audi AG", "AUDI AG", "Audi Sport"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Fahrwerk",
            "working student Ingolstadt",
            "Masterarbeit Fahrzeugtechnik",
            "intern engineer Ingolstadt",
        ],
    },
    {
        "name": "Porsche",
        "name_variants": ["Porsche", "Porsche AG", "Dr. Ing. h.c. F. Porsche", "Porsche Engineering"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Fahrdynamik",
            "working student Stuttgart",
            "Masterarbeit Antrieb",
        ],
    },
    {
        "name": "Daimler Truck",
        "name_variants": ["Daimler Truck", "Daimler Trucks", "Daimler Truck AG", "Mercedes-Benz Trucks"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum engineering",
            "working student truck",
            "Masterarbeit Nutzfahrzeug",
        ],
    },
    {
        "name": "Opel",
        "name_variants": ["Opel", "Opel Automobile", "Stellantis", "Opel Automobile GmbH"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum automotive",
            "intern engineer Rüsselsheim",
        ],
    },
    # ── Tier-1 Suppliers (LinkedIn discovery) ────────────────
    {
        "name": "Robert Bosch",
        "name_variants": ["Bosch", "Robert Bosch", "Robert Bosch GmbH", "Bosch Engineering"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum engineering",
            "working student automotive",
            "Masterarbeit Elektrotechnik",
        ],
    },
    {
        "name": "Vitesco Technologies",
        "name_variants": ["Vitesco", "Vitesco Technologies", "Vitesco Technologies GmbH"],
        "search_terms": [
            "Werkstudent Elektromobilität",
            "Praktikum EV powertrain",
            "working student electric",
            "Masterarbeit Antriebselektronik",
        ],
    },
    {
        "name": "Hella",
        "name_variants": ["Hella", "HELLA", "Forvia HELLA", "HELLA GmbH", "Forvia"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum electronics",
            "working student Lippstadt",
            "Masterarbeit Fahrzeugelektronik",
        ],
    },
    {
        "name": "Mahle",
        "name_variants": ["Mahle", "MAHLE", "MAHLE GmbH", "MAHLE International"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum thermal",
            "working student Stuttgart",
            "Masterarbeit Thermomanagement",
        ],
    },
    {
        "name": "ElringKlinger",
        "name_variants": ["ElringKlinger", "Elring Klinger", "ElringKlinger AG"],
        "search_terms": [
            "Werkstudent EV",
            "Praktikum fuel cell",
            "Masterarbeit Brennstoffzelle",
            "working student electric",
        ],
    },
    # ── Simulation & Test Specialists ─────────────────────────
    {
        "name": "AVL",
        "name_variants": ["AVL", "AVL List", "AVL Deutschland", "AVL Software and Functions"],
        "search_terms": [
            "Werkstudent Simulation",
            "Praktikum Motorenentwicklung",
            "working student powertrain",
            "Masterarbeit Simulation",
        ],
    },
    {
        "name": "dSPACE",
        "name_variants": ["dSPACE", "dSPACE GmbH", "dspace"],
        "search_terms": [
            "Werkstudent Simulation",
            "Praktikum HIL",
            "working student ADAS",
            "Masterarbeit Embedded",
        ],
    },
    {
        "name": "ETAS",
        "name_variants": ["ETAS", "ETAS GmbH", "ETAS Group"],
        "search_terms": [
            "Werkstudent Embedded",
            "Praktikum automotive software",
            "working student Stuttgart",
        ],
    },
    {
        "name": "Horiba",
        "name_variants": ["Horiba", "Horiba Europe", "Horiba FuelCon", "Horiba MIRA"],
        "search_terms": [
            "Werkstudent testing",
            "Praktikum Erprobung",
            "working student measurement",
        ],
    },
    # ── Engineering Services ──────────────────────────────────
    {
        "name": "Ricardo",
        "name_variants": ["Ricardo", "Ricardo plc", "Ricardo Germany", "Ricardo GmbH"],
        "search_terms": [
            "engineer Germany",
            "Werkstudent automotive",
            "Praktikum powertrain",
        ],
    },
    {
        "name": "Bertrandt",
        "name_variants": ["Bertrandt", "Bertrandt AG", "Bertrandt GmbH"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Fahrzeugentwicklung",
            "working student automotive Bavaria",
        ],
    },
    {
        "name": "Hyundai Motor Europe",
        "name_variants": ["Hyundai", "Hyundai Motor Europe", "Hyundai Motor", "Kia Europe"],
        "search_terms": [
            "Werkstudent engineer Frankfurt",
            "Praktikum automotive",
            "working student EV",
            "intern engineer Offenbach",
        ],
    },
    # ── Former Personio tenants (migrated to SAP SF / own portals) ───────────
    # Added 2026-05-05 when Personio deprecated *.jobs.personio.{de,com} wildcard.
    {
        "name": "EDAG Engineering",
        "name_variants": ["EDAG", "EDAG Engineering", "EDAG Group", "EDAG Engineering GmbH"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Fahrzeugentwicklung",
            "working student automotive",
            "Masterarbeit Fahrzeug",
        ],
    },
    {
        "name": "Knorr-Bremse",
        "name_variants": ["Knorr-Bremse", "Knorr Bremse", "Knorr-Bremse AG", "Knorr-Bremse Group"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Bremssystem",
            "working student Munich",
            "Masterarbeit Brake",
        ],
    },
    {
        "name": "MAN Truck & Bus",
        "name_variants": ["MAN", "MAN Truck", "MAN Truck & Bus", "MAN SE"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Nutzfahrzeug",
            "working student truck",
            "Masterarbeit Antrieb",
        ],
    },
    {
        "name": "IAV GmbH",
        "name_variants": ["IAV", "IAV GmbH", "IAV Automotive Engineering"],
        "search_terms": [
            "Werkstudent Simulation",
            "Praktikum Fahrzeugtechnik",
            "working student automotive",
            "Masterarbeit Antriebsentwicklung",
        ],
    },
    {
        "name": "FEV Group",
        "name_variants": ["FEV", "FEV Group", "FEV GmbH", "FEV Europe"],
        "search_terms": [
            "Werkstudent Simulation",
            "Praktikum Motorenentwicklung",
            "working student powertrain",
            "Masterarbeit Verbrenner",
        ],
    },
    {
        "name": "Expleo",
        "name_variants": ["Expleo", "Expleo Group", "Expleo Germany", "AKKA Technologies"],
        "search_terms": [
            "Werkstudent engineering",
            "Praktikum Fahrzeugtechnik",
            "working student automotive",
        ],
    },
    {
        "name": "ALTEN",
        "name_variants": ["ALTEN", "ALTEN GmbH", "ALTEN Group", "ALTEN Technology"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum automotive",
            "working student engineering",
        ],
    },
    # ── Workday 422 companies not yet on LinkedIn list ─────────
    # Their Workday APIs are locked (422); LinkedIn is primary discovery path.
    {
        "name": "Continental",
        "name_variants": ["Continental", "Continental AG", "Continental Automotive", "Conti"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Fahrzeugtechnik",
            "working student automotive",
            "Masterarbeit Elektronik",
        ],
    },
    {
        "name": "ZF Friedrichshafen",
        "name_variants": ["ZF", "ZF Friedrichshafen", "ZF Group", "ZF AG"],
        "search_terms": [
            "Werkstudent Fahrwerk",
            "Praktikum Getriebe",
            "working student driveline",
            "Masterarbeit Antrieb",
        ],
    },
    {
        "name": "Schaeffler",
        "name_variants": ["Schaeffler", "Schaeffler AG", "Schaeffler Group", "LuK"],
        "search_terms": [
            "Werkstudent Ingenieur",
            "Praktikum Elektromobilität",
            "working student bearing",
            "Masterarbeit E-Mobilität",
        ],
    },
    {
        "name": "Infineon Technologies",
        "name_variants": ["Infineon", "Infineon Technologies", "Infineon Technologies AG"],
        "search_terms": [
            "Werkstudent Simulation",
            "Praktikum Halbleiter",
            "working student Regensburg",
            "Masterarbeit Power Electronics",
        ],
    },
    {
        "name": "Harman International",
        "name_variants": ["Harman", "Harman International", "HARMAN", "Samsung Harman"],
        "search_terms": [
            "Werkstudent automotive",
            "Praktikum ADAS",
            "working student connected car",
        ],
    },
])
