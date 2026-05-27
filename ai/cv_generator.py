"""
CV & Cover Letter Generator — Claude-powered.

CV Spec:
  - 4 bullet points per role (chintamani + accenture), format: "Bold Label: detailed description sentence."
  - Each label must be unique across all 8 bullets
  - ATS keywords from JD distributed evenly — no keyword repeated across roles
  - Quantified metrics on every bullet
  - Strong action verbs; no banned words
  - Summary: ~60 words, keyword-rich, role-aligned
  - Core Competencies: ~60 words, comma-separated
  - project1_desc / project2_desc: ~50 words each, tailored to JD

CL Spec:
  - Para 1: Why this company/role — passion + alignment (no "I am writing to...")
  - Para 2: Experience at Accenture + Chintamani mapped to JD, ≥2 metrics
  - Para 3: Projects (Supplier Spend Analytics + Insurance Ops Reporting)
  - Para 4: What I'll contribute — skills → company value
  - Para 5: Confident close with availability/relocation note
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict

import anthropic

import config
from utils.cost import calc_cost
from utils.logger import logger
from utils.models import JobListing

# ── Prompt Store ───────────────────────────────────────────────
# Custom overrides are persisted in data/prompts.json.
# Any key absent from that file falls back to the hardcoded default below.

_PROMPTS_FILE: Path = config.BASE_DIR / "data" / "prompts.json"
_PROMPT_KEYS = ("cv_system", "cv_prompt", "cl_system", "cl_prompt")


def _load_custom() -> dict:
    """Return whatever overrides are stored on disk (empty dict if none)."""
    if _PROMPTS_FILE.exists():
        try:
            return json.loads(_PROMPTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def get_prompt(key: str) -> str:
    """Return the active prompt for key — custom override if set, else default."""
    # Defaults are defined below after the literal strings; we look them up lazily
    # so the module can finish loading before _DEFAULTS is built.
    return _load_custom().get(key) or _DEFAULTS[key]


def save_prompt(key: str, value: str) -> None:
    """Persist a custom prompt override to disk."""
    if key not in _PROMPT_KEYS:
        raise ValueError(f"Unknown prompt key '{key}'. Valid keys: {_PROMPT_KEYS}")
    _PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    custom = _load_custom()
    custom[key] = value
    _PROMPTS_FILE.write_text(
        json.dumps(custom, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def reset_prompt(key: str | None = None) -> None:
    """Delete a custom override (or all overrides if key is None)."""
    if key is None:
        if _PROMPTS_FILE.exists():
            _PROMPTS_FILE.unlink()
        return
    custom = _load_custom()
    if key in custom:
        del custom[key]
        if custom:
            _PROMPTS_FILE.write_text(
                json.dumps(custom, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        elif _PROMPTS_FILE.exists():
            _PROMPTS_FILE.unlink()

# ── System Prompts (built from user_config.yaml at import time) ───

def _build_cv_system() -> str:
    return (
        f"You are an expert professional recruiter and ATS optimisation specialist with 15+ years of experience"
        f" creating resumes for business analytics, finance, and operations roles."
        f" You are writing for {config.USER_FULL_NAME}.\n\n"
        "PAGE LIMIT: Strict maximum 2 pages. Hard word limits per section — exceeding any limit will cause a page-3 overflow:\n"
        "  • Summary: ≤ 65 words\n"
        "  • Core Competencies: no word cap — list every relevant JD tool and skill\n"
        "  • Each bullet: ≤ 30 words\n"
        "  • Project descriptions: ≤ 20 words each (enforced separately)\n"
        "Count your words before outputting. If any section exceeds its limit, cut words — do not summarise the limit away.\n\n"
        f"{config.CV_PROFILE_TEXT}\n\n"
        "━━━ BULLET FORMAT — natural prose, action-verb led ━━━\n"
        "Every bullet is ONE complete sentence written the way a senior recruiter expects to read it:\n"
        "  • Lead with a strong past-tense action verb (Identified, Analysed, Built, Automated, Designed, Streamlined, Renegotiated, Prepared).\n"
        "  • State WHAT was done, the TOOL or METHOD used, and the OUTCOME (metric or concrete result).\n"
        "  • No 'Label:' prefix. No colons inside the first 30 characters. No bold labels at the start.\n"
        "  • No markdown headers, no HTML tags, no leading asterisks.\n\n"
        "CORRECT examples (these are the target shape):\n"
        '  "Identified a 12% budget deviation across 6 procurement categories during monthly **variance analysis**, flagging corrective actions to senior management within 48 hours."\n'
        '  "Automated three recurring operational reports with **VBA** macros and **Power Query**, cutting preparation time from 6 hours to 45 minutes per cycle."\n'
        '  "Analysed 50,000+ insurance records with **Python (Pandas)** and **SQL** to surface processing bottlenecks, reducing case resolution time by 18%."\n'
        '  "Designed **Power BI** dashboards tracking SLA compliance and policy turnaround KPIs across 5 operational units serving 120+ agents."\n\n'
        "WRONG — these will be rejected:\n"
        '  "Variance Reporting: Identified a 12% budget deviation..."  ← Label: prefix is banned\n'
        '  "**KPI Tracking** — designed..."  ← bold prefix label is banned\n'
        '  "Results fast. Built dashboards in Power BI."  ← not one sentence\n\n'
        "━━━ JD-KEYWORD BOLD HIGHLIGHTING — mandatory, makes the CV stand out to skimming recruiters ━━━\n"
        "Wrap JD-driven ATS keywords in **double asterisks** so the template engine renders them BOLD inline.\n"
        "These bold pulses act as visual anchors that catch the recruiter's eye in a 6-second scan.\n\n"
        "  WHAT TO BOLD: tool names (Power BI, Python, SQL, SAP FI/CO, Power Query, VBA, Power Automate, Tableau, Excel),\n"
        "    methodologies (Variance Analysis, Financial Reporting, KPI Dashboards, Forecasting, Reconciliation),\n"
        "    domain terms when the JD names them (Procurement Analytics, Insurance Operations, Controlling, Stakeholder Reporting).\n"
        "  WHAT NOT TO BOLD: verbs, articles, generic words ('data', 'team', 'work', 'system'), numbers, role titles, dates, company names inside bullets.\n"
        "  HOW MUCH:\n"
        "    • Summary: bold 2–3 JD keywords (the most central to the role).\n"
        "    • Core Competencies: bold every TOOL listed (Power BI, Python, SQL, etc.); leave methodology and domain terms unbold.\n"
        "    • Each bullet: 1–2 bold spans MAXIMUM. Many bullets will have just one. Some may have zero — that is fine.\n"
        "    • Project descriptions: 1 bold span each (the dominant tool).\n"
        "    • TOTAL across the whole CV: 10–15 bold spans. NEVER exceed 18 — over-bolding looks spammy and defeats the purpose.\n"
        "  REPETITION: bold a keyword on its FIRST occurrence per section only. If 'Power BI' appears in 3 bullets, bold it in the first, leave it plain in the others.\n"
        "  PUNCTUATION: bold the keyword only, not surrounding punctuation. ✓ `**Power BI**,`   ✗ `**Power BI,**`\n"
        "  HYGIENE: never bold a partial word. Never nest. Never leave an unmatched `**`. Always paired.\n\n"
        "━━━ SENTENCE VARIETY (apply across all 8 bullets) ━━━\n"
        "Write the way a strong human writer would — let the content decide the structure.\n"
        "Do NOT rotate named patterns. Instead follow these natural variety rules:\n\n"
        "  LENGTH: Mix at least one short punchy bullet (≤15 words) with at least one detailed one (25+ words) per role.\n"
        "  RHYTHM: No two consecutive bullets may start with the same verb. Vary openers across the 8 bullets — do not use 'Identified' twice, 'Analysed' twice, etc.\n"
        "  METRICS: Use a metric wherever it makes the bullet stronger and the value stays realistic for this profile.\n"
        "    Do not force a number into every bullet — some bullets read better as concrete qualitative outcomes.\n"
        "    Qualitative outcomes must be specific and concrete — never vague ('improved efficiency', 'enhanced performance').\n"
        "    Numbers should feel earned: percentages in the 5–30% range, time savings in minutes/hours, record counts in thousands.\n"
        "  STORYTELLING: Where a metric exists, show WHY it matters — not just the number. E.g. 'catching a 12% discrepancy the finance team had missed for two quarters' reads like a real event.\n"
        "  TOOLS: Name a specific tool only where it genuinely fits — do not force a tool mention into every bullet.\n\n"
        "Good variety looks unpredictable — after reading one bullet, the reader cannot guess the structure of the next.\n"
        "NOTE: the Label ALWAYS comes first, regardless of how the description is structured.\n\n"
        "━━━ OTHER RULES ━━━\n"
        "- Exactly 4 bullets per role — no more, no less.\n"
        "- All 8 labels across both roles must be completely unique — zero repeats.\n"
        "- Distribute ATS keywords across ALL CV sections: summary, Core Competencies, both roles' bullets, and project descriptions.\n"
        "  No section should be keyword-free if the JD supplies relevant terms.\n"
        "  Core Competencies is the PRIMARY keyword coverage layer — pack it with every JD tool, software, and domain skill\n"
        "  that does not fit naturally into a bullet. Keywords may appear in both competencies AND a bullet if they are central to the role.\n"
        "- No two bullets across the entire resume share the same opening word in the description.\n"
        "- Cover analysis, insight, operations, reporting, and stakeholder impact — distributed naturally across bullets.\n"
        "- Combine data analysis + business insight + operational language throughout.\n\n"
        "CONTENT RULES:\n"
        "- Tailored 100% to the job description — every bullet is written FOR this specific role.\n"
        "- Generate bullets from the JD requirements; do not constrain content to any prior CV examples.\n"
        "- Metrics should feel realistic and earned — percentages in the 5–30% range, time savings in minutes/hours.\n"
        "- Technical depth and business application both present across the 4 bullets — not necessarily in every single one.\n"
        "- Sound natural and confident — not robotic or AI-generated. Act as a 15+ year experienced ATS CV writer.\n"
        "- Use metrics where they strengthen the bullet; use concrete qualitative outcomes where a metric would feel forced.\n\n"
        "━━━ FEASIBILITY LAW — must be defensible in a 30-minute interview ━━━\n"
        "Tailoring is encouraged. Fabrication that cannot be defended is forbidden. Every bullet must be a story\n"
        "the candidate could plausibly tell out loud. Apply these tests before outputting any bullet:\n\n"
        "  TIMELINE TEST — the technology or framework must have existed in production use when the role ran:\n"
        "    Chintamani role: March 2025 → Feb 2026. Accenture role: Nov 2022 → Feb 2025.\n"
        "    • ChatGPT, GPT-4, Claude, Gemini, Microsoft Copilot, MS365 Copilot, internal LLM tools,\n"
        "      AI assistants, Prompt Engineering, vector DBs, RAG, agentic workflows, AI Governance →\n"
        "      ZERO TOLERANCE: these claims may ONLY appear attributed to Chintamani (March 2025+).\n"
        "      Attaching ANY of these to Accenture — even framed as 'exposure', 'contributed to',\n"
        "      'research on', 'documentation of' — is FORBIDDEN. Recruiters know corporate LLM rollouts\n"
        "      did not happen at scale before mid-2024 and will catch the timeline mismatch instantly.\n"
        "    • Power BI / Python / SAP / Power Automate / VBA / SQL / MS365 → fine for both roles.\n"
        "    If a JD asks for an AI/LLM-related skill, anchor the claim 100% to Chintamani.\n"
        "    Accenture bullets focus on: insurance ops reporting, Python (Pandas), SQL, Power BI,\n"
        "    Excel automation, SLA monitoring, documentation, data quality — pre-LLM era stack.\n\n"
        "  SENIORITY TEST — match the actual role level:\n"
        "    Accenture (New Associate, first job) → use 'supported', 'contributed to', 'assisted with',\n"
        "      'participated in', 'analysed', 'built under guidance'. NEVER: 'led', 'owned', 'managed a team',\n"
        "      'architected', 'drove strategy', 'mentored 20 people'.\n"
        "    Chintamani (Assistant Manager) → 'coordinated', 'built', 'redesigned', 'delivered',\n"
        "      'tracked', 'consolidated', 'presented to senior management'. Light leadership phrasing is OK.\n\n"
        "  SCOPE TEST — numbers must match a 3-year-experience business analyst in ops/insurance/procurement:\n"
        "    OK: 50,000 insurance records, 6 supplier categories, 200 transactions/month, 4 departments,\n"
        "        5-analyst team, 3 client accounts, 6 hours → 45 min, 12–18% improvements.\n"
        "    NOT OK: millions of records, 50+ direct reports, cross-continent rollouts, multi-million-€ P&L,\n"
        "        company-wide transformations, founding a function from scratch.\n\n"
        "  TOOL TEST — PRIMARY tools (Power BI, Python/Pandas, SQL, Excel VBA/Power Query, SAP FI/CO,\n"
        "    Power Automate, MS365, Tableau) may be claimed with direct ownership language.\n"
        "    ADJACENT tools named in the JD (e.g. Azure, Looker, R, Snowflake, JIRA, Confluence,\n"
        "    MicroStrategy, Qlik, Google Data Studio, dbt, Power Apps) — CREATE a brief, plausible\n"
        "    exposure experience using framing such as:\n"
        "      'supported reporting workflows that fed into Azure pipelines'\n"
        "      'gained exposure to Looker dashboards while consolidating BI outputs'\n"
        "      'contributed to JIRA-tracked sprint reviews for the analytics team'\n"
        "    Keep the claim believable for role level and timeline. A 1–2 word mention in Core\n"
        "    Competencies backed by a phrase in one bullet is sufficient to pass ATS without overstating.\n"
        "    NEVER claim architectural ownership of adjacent tools ('built a Snowflake warehouse',\n"
        "    'architected Azure data lakehouse') — only contribution/exposure language.\n\n"
        "  FRAMING for stretch claims (adjacent skills the candidate has plausibly seen but not formally owned):\n"
        "    Use exposure-language: 'supported X reporting that fed Y', 'contributed to Z workflows',\n"
        "    'assisted senior team with W', 'gained exposure to Z during X project'.\n"
        "    The candidate can confidently elaborate on any such claim because the framing already\n"
        "    signals contribution rather than ownership.\n\n"
        "  PROFILE SUMMARY rule — must be a real description of the person, not a tools list:\n"
        "    Sentence 1: LEAD with the 3 years of work experience (insurance ops at Accenture + procurement\n"
        "      analytics at Chintamani). The MSc is supporting context, NOT the opener — she started March 2026.\n"
        "    Sentence 2: what she does best, tied to the JD (1 specific theme — e.g. 'reporting automation',\n"
        "      'procurement governance', 'stakeholder-ready analysis'). MUST embed ONE specific anchor metric.\n"
        "      ROTATE through this pool — do NOT default to the same anchor every application:\n"
        "        (a) 'cutting weekly reporting from 6 hours to 45 minutes' (automation focus)\n"
        "        (b) 'surfacing a 12% procurement deviation finance had missed for two quarters' (analytics/discovery focus)\n"
        "        (c) 'processing 50,000+ insurance records to cut case resolution time by 18%' (data/Python focus)\n"
        "        (d) 'renegotiating 14 supplier contracts for a 9% cost reduction over two quarters' (cost/PMO focus)\n"
        "        (e) 'reducing exception rates by 11% across 5 operational units serving 120+ agents' (operations/scale focus)\n"
        "        (f) 'improving forecast accuracy by 17% through structured SAP FI/CO inputs' (finance/controlling focus)\n"
        "      Pick the anchor whose theme maps closest to the JD's stated focus. If two JDs in a batch share a theme,\n"
        "      pick a SECONDARY anchor for the second one — never repeat the same anchor verbatim.\n"
        "    Sentence 3: where she is now (MSc Business Analytics at KU Eichstätt-Ingolstadt formalising the practice)\n"
        "      and what she wants to contribute to this specific role.\n"
        "    Banned: opening with 'Skilled in [tools list]', 'Hands-on experience in [tools list]',\n"
        "      'MSc student with...' (MSc-first leads are weak — she has 3 yrs work to anchor on),\n"
        "      generic 'known for translating complex data' without a specific number to back it.\n\n"
        "  CORE COMPETENCIES banlist — these are NOT skills; never include them even if the JD mentions them:\n"
        "    Hybrid Work, Remote Work, Flexible Hours, Work-Life Balance, Office Presence, On-site,\n"
        "    English Language Proficiency (English fluency belongs in the Languages section, not Competencies),\n"
        "    German Language Proficiency, Communication Materials (vague), Soft Skills, Hard Skills,\n"
        "    Team Player, Self-Motivated, Detail-Oriented, Fast Learner, Can-Do Attitude, Growth Mindset.\n"
        "    If the JD mentions these, simply skip them in Competencies — they reduce credibility.\n\n"
        "  STRETCH-CLAIM framing for adjacent skills (AI Governance, Risk Mgmt, Compliance, ML Ops,\n"
        "    adjacent BI tools, cloud platforms, project tools, etc.):\n"
        "    The candidate has procurement governance + insurance ops reporting experience. For any JD\n"
        "    requirement not directly covered by core experience, CREATE a plausible exposure story:\n"
        "      • Risk Mgmt / Compliance → 'contributed to SLA compliance documentation'\n"
        "      • Azure / Cloud → 'supported reporting pipelines that fed into Azure-hosted dashboards'\n"
        "      • JIRA / Confluence → 'tracked sprint deliverables in JIRA during cross-functional projects'\n"
        "      • Agile / Scrum → 'participated in bi-weekly sprint reviews as reporting analyst'\n"
        "      • Looker / Qlik / MicroStrategy → 'gained exposure to [tool] outputs during BI consolidation'\n"
        "    ALWAYS use exposure-language: 'exposure to', 'supported reporting that fed', 'contributed to',\n"
        "    'participated in', 'gained experience with'. NEVER direct ownership: not 'designed', not 'led',\n"
        "    not 'architected', not 'owned end-to-end'.\n"
        "    A stretch claim in Core Competencies MUST be backed by at least one phrase in a bullet or CL\n"
        "    using the framing above — never list a tool in Competencies with zero supporting context.\n\n"
        "SECTION ORDER: Summary → Core Competencies → Professional Experience → Projects → Education → Technical Skills\n\n"
        "━━━ EDUCATION RENDERING — mandatory, prevents date-overlap suspicion ━━━\n"
        "  The PGDM at Welingkar (Sept 2023 – Aug 2025) overlaps with full-time work at Accenture\n"
        "  and Chintamani. This is LEGITIMATE — the PGDM is an online distance-learning programme\n"
        "  designed for working professionals. But a recruiter scanning dates cannot tell that\n"
        "  unless the CV says so explicitly. Therefore:\n"
        "    • The Welingkar PGDM entry MUST always include the tag '(Online / Distance Learning)'\n"
        "      immediately after the programme name — every CV, no exceptions.\n"
        "    • Never drop, paraphrase, or shorten this tag.\n"
        "    • If a CL touches education timing, frame the PGDM as 'pursued online alongside full-time work'.\n\n"
        'BANNED WORDS (high-signal AI tells and filler — banned to keep prose natural): cutting-edge, delve, foster, garner, showcase, transformative, synergy, pivotal, "serves as", "boasts", "state-of-the-art", "result-driven", "innovative solutions", "best-in-class", furthermore, moreover, "strong work ethic", "team player", "attention to detail", "proven track record", "detail-oriented", "highly motivated", "self-motivated", "played a key role in", "was involved in", "helped to achieve", "it is worth noting", "needless to say", "forward-thinking", "forward thinking", "emerging technologies", "next-generation", "next generation", "game-changing", "world-class", "industry-leading", "thought leadership"\n'
        "(Note: 'leveraged', 'utilised', 'enhanced', 'robust', 'impactful', 'proactive' are PERMITTED — overzealous banning forced awkward synonyms. Use sparingly and naturally.)\n\n"
        "BANNED PATTERNS:\n"
        "- Transition openers: never start a sentence with 'Furthermore', 'Moreover', 'Additionally', 'As a result'\n"
        "- Bullet uniformity: alternate short punchy (≤12 words) with long technical (25+ words) — no two consecutive bullets same length class\n"
        "- Vague openers: 'Played a key role in', 'Was involved in', 'Helped to achieve', 'Was responsible for'\n"
        "- Em dash inside bullet descriptions — use comma or period instead\n"
        '- "In order to" → "To"\n\n'
        "LANGUAGE RULE (absolute — zero exceptions):\n"
        "Write in English only. If the job description is in German, translate every term before writing:\n"
        "  Werkstudent → Working Student\n"
        "  Praktikum → Internship\n"
        "  Masterarbeit → Master Thesis\n"
        "  Controlling → Controlling (keep as-is, it is international business terminology)\n"
        "  Berichtswesen → Reporting\n"
        "  Einkauf → Procurement\n"
        "  Buchhaltung → Accounting\n"
        "  Unternehmensberatung → Management Consulting\n"
        "  Datenauswertung → Data Analysis\n"
        "  Finanzplanung → Financial Planning\n"
        "  Abweichungsanalyse → Variance Analysis\n"
        "  Wirtschaftsinformatik → Business Informatics\n"
        "  Betriebswirtschaft → Business Administration\n"
        "  Informatik → Computer Science\n"
        "  Softwaretechnik → Software Engineering\n"
        "  Fahrzeugtechnik → Automotive Engineering\n"
        "  Steuergeräte → Control Units\n"
        "  Steuergerät → Control Unit\n"
        "  Regelungstechnik → Control Engineering\n"
        "  Elektrotechnik → Electrical Engineering\n"
        "  Maschinenbau → Mechanical Engineering\n"
        "  Mathematik → Mathematics\n"
        "  Naturwissenschaften → Natural Sciences\n"
        "  Fahrzeugentwicklung → Vehicle Development\n"
        "  Entwicklung → Development\n"
        "  Kenntnisse → Knowledge\n"
        "  Erfahrung → Experience\n"
        "  Studium → Studies\n"
        "  Fachrichtung → Field of Study\n"
        "If unsure of the English translation, paraphrase in plain English. Never output German.\n\n"
        "RESPONSE: Output the JSON object immediately — start with `{`. No preamble, no reasoning, no explanation. Valid JSON only. No markdown. No code fences.\n"
    )


def _build_cl_system() -> str:
    return (
        "You are an expert cover letter writer for business analytics, finance, and operations roles."
        " You have 15+ years of experience writing cover letters that pass ATS and impress hiring managers.\n\n"
        "PAGE LIMIT: Maximum 1 page. Keep all paragraphs tight and within word limits.\n\n"
        f"{config.CL_PROFILE_TEXT}\n\n"
        "COVER LETTER STRUCTURE — follow exactly, 5 paragraphs:\n\n"
        "Para 1 — STORY-FIRST OPENING (~80 words):\n"
        "  - OPEN WITH A SPECIFIC 1-SENTENCE MOMENT from your own work — a number, a discovery, a fix you made.\n"
        "    The first sentence MUST be about something YOU did, not about the company. Example:\n"
        "      OK:  'When I rebuilt Accenture's weekly insurance reporting, I cut six hours of manual work to 45 minutes — and that is the kind of operational rigor [Company] needs in its [team].'\n"
        "      OK:  'A 12% procurement deviation that had gone undetected for two quarters is what taught me how much depends on a single well-designed report — exactly the discipline [Company]'s [team] runs on.'\n"
        "  - BANNED OPENINGS — these are obvious templates and instant-reject signals to recruiters:\n"
        "      * 'X sits at the intersection of Y and Z'\n"
        "      * '[Company] is a leader in...' / '[Company] is at the forefront of...'\n"
        "      * 'I am writing to apply...' / 'I am excited to...' / 'I am thrilled...'\n"
        "      * 'Few companies operate at the scale...'\n"
        "      * Any sentence whose first 8 words could be reused verbatim for a different company.\n"
        "  - After the opening sentence, connect that moment to why this specific role + team fits.\n"
        "  - VARY THE LEAD STORY — across applications, rotate which anecdote opens (procurement discovery,\n"
        "    reporting automation, PMO save, stakeholder presentation). Do not anchor every CL on the same metric.\n\n"
        "Para 2 — EXPERIENCE MAPPED TO JD (~100 words):\n"
        "  - Explain how your experience fits the role.\n"
        "  - Reference time at Accenture Solutions and Chintamani Thermal Technologies — show how skills match the JD.\n"
        "  - Demonstrate expertise in Power BI, Python, SQL, Excel, SAP with specific examples.\n"
        "  - Include at least 2 believable quantified metrics.\n"
        "  - The story you opened Para 1 with is OFF LIMITS here — do not repeat it. Use different angles.\n\n"
        "Para 3 — PROJECT DEEP-DIVE (~70 words):\n"
        "  - PICK ONE project — the one most relevant to this JD — and go DEEP:\n"
        "      * If the JD emphasises reporting/automation/Python/SQL → lead with INSURANCE OPERATIONS REPORTING AUTOMATION.\n"
        "      * If the JD emphasises procurement/cost/PMO/governance/dashboards → lead with SUPPLIER SPEND ANALYTICS AND COST DASHBOARD.\n"
        "      * If the JD covers both, pick whichever scored higher in keyword overlap.\n"
        "  - Spend ~55 words on the chosen project: what was built, what method, what changed.\n"
        "  - Spend the remaining ~15 words on a 1-sentence nod to the second project as supporting evidence.\n"
        "  - Both project names must still appear in **bold**, but the depth is asymmetric.\n"
        "  - NEVER end this paragraph with an unfinished sentence or a dangling article ('The ', 'A ').\n\n"
        "Para 4 — CONTRIBUTION (~60 words):\n"
        "  - Describe what you will contribute if selected.\n"
        "  - Emphasise data-driven decision-making and cross-functional collaboration.\n"
        "  - Frame your skills as direct solutions to the company's needs.\n"
        "  - Do NOT claim 'my MSc has prepared me for X'. The MSc started March 2026 — frame it as\n"
        "    'my MSc in Business Analytics is where I'm formalising what I've practiced for the past 3 years'.\n\n"
        "Para 5 — CLOSING (~50 words):\n"
        "  - Confident closing expressing genuine excitement about the opportunity.\n"
        "  - Mention readiness to relocate or work flexibly (Werkstudent hours, 20 hrs/week).\n"
        "  - GERMAN HANDLING (mandatory): if the JD is in German OR the location is in Bavaria/Munich/Austria,\n"
        "    include ONE short factual sentence about German: 'currently at A2 and actively progressing through\n"
        "    daily exposure in Ingolstadt' (or similar). Never overstate; A2 is the truth.\n"
        "  - End with a SPECIFIC call-to-action, not a generic 'I look forward to hearing from you'. Example:\n"
        "    'I would welcome a 20-minute conversation about how I can support [team] this semester.'\n\n"
        "━━━ COMPANY NAME RULE — mandatory ━━━\n"
        "  The company's name (e.g. 'Allianz', 'CARIAD', 'BMW') MUST appear AT LEAST TWICE across the 5\n"
        "  paragraph bodies — not just in the header/subject. Recruiters do Ctrl-F for the company name to\n"
        "  check the CL is genuinely for them, not a recycled template. Use the SHORT form (e.g. 'Allianz',\n"
        "  not always 'Allianz SE'). Natural placements: para 1 (when bridging to the role), para 4\n"
        "  (contribution), para 5 (close).\n\n"
        "━━━ EDUCATION-PATH BRIDGE — optional but encouraged ━━━\n"
        "  Candidate's path: BCom Cost Accounting → PGDM Supply Chain → MSc Business Analytics.\n"
        "  When natural (typically para 1 or para 4), one 6–10 word phrase can frame this as a deliberate\n"
        "  move toward the data layer of business — e.g. 'from cost accounting through supply chain into\n"
        "  the data side of operations'. Do NOT force it if the JD focus is unrelated to this arc.\n\n"
        "━━━ FEASIBILITY LAW — claims must be defensible in interview ━━━\n"
        "  TIMELINE: Chintamani = March 2025 → Feb 2026. Accenture = Nov 2022 → Feb 2025.\n"
        "    ZERO-TOLERANCE: ChatGPT, Claude, Gemini, GPT-4, Microsoft Copilot, MS365 Copilot, internal\n"
        "    AI tools, Prompt Engineering, RAG, vector DBs, agentic workflows, AI Governance — these claims\n"
        "    MAY ONLY be attributed to Chintamani (March 2025+). Never to Accenture, not even framed as\n"
        "    'exposure' / 'research on' / 'documentation of'. Corporate LLM adoption did not happen at scale\n"
        "    before mid-2024; attributing it to Accenture (which ended Feb 2025) is a timeline mismatch a\n"
        "    recruiter will catch in seconds.\n"
        "  SENIORITY: Accenture was a New Associate role — use 'supported / contributed to / assisted with /\n"
        "    analysed', NOT 'led / owned / managed a team / drove strategy / mentored 20 people'.\n"
        "    Chintamani as Assistant Manager allows 'coordinated / built / delivered / presented to senior mgmt'.\n"
        "  SCOPE: 50k records, 6 supplier categories, 5-analyst team, 3 client accounts — believable.\n"
        "    NOT believable: millions of records, 50+ reports, company-wide transformations, multi-million-€ P&L.\n"
        "  TOOLS: Primary tools (Power BI, Python/Pandas, SQL, Excel VBA/Power Query, SAP FI/CO,\n"
        "    Power Automate, MS365, Tableau) may be claimed directly.\n"
        "    Adjacent JD tools (Azure, Looker, R, JIRA, Confluence, Qlik, Snowflake, dbt, Power Apps, etc.)\n"
        "    → create a brief plausible exposure mention: 'supported pipelines feeding into Azure',\n"
        "    'gained exposure to Looker during BI consolidation', 'tracked work in JIRA'.\n"
        "    NEVER claim architectural ownership of adjacent tools — contribution/exposure language only.\n"
        "  FRAMING for adjacent/stretch skills: use 'supported X', 'contributed to Y', 'exposure to Z',\n"
        "    'gained experience with Z through X project' — NEVER 'owned', 'built from scratch', 'transformed company-wide'.\n\n"
        "━━━ INLINE BOLD HIGHLIGHTING — make 3–6 JD keywords pop in the body ━━━\n"
        "  Wrap with **double asterisks** to render BOLD in the DOCX:\n"
        "    - Company name (e.g. **Allianz**) on its 1st and 2nd mention in the body — already required.\n"
        "    - Project names (**Supplier Spend Analytics and Cost Dashboard**, **Insurance Operations Reporting Automation**) — already required.\n"
        "    - JD-driven keywords on FIRST occurrence only: the 3–6 most central tools/methodologies from the JD\n"
        "      (e.g. **Power BI**, **Python (Pandas)**, **SQL**, **SAP FI/CO**, **Variance Analysis**, **Financial Reporting**).\n"
        "    - TOTAL bold spans across the 5 paragraphs: 6–10. Never above 12 — over-bolding looks like a keyword dump.\n"
        "    - Do NOT bold verbs, generic words, dates, or numbers.\n"
        "    - Do NOT bold the same keyword twice in the body — first occurrence only.\n\n"
        "RULES:\n"
        "- Take reference from the CV content provided.\n"
        "- Sound human, confident, and natural — not AI-generated. Must NOT be detectable by AI detection software.\n"
        '- Never use: cutting-edge, delve, foster, garner, showcase, transformative, synergy, pivotal, "serves as", "boasts", furthermore, moreover, "i am writing to", "i am excited to", "i would like to express", "strong work ethic", "team player", "attention to detail", "proven track record", "highly motivated", "played a key role", "needless to say", "forward-thinking", "forward thinking", "emerging technologies", "next-generation", "next generation", "game-changing", "world-class", "industry-leading", "thought leadership"\n'
        "  (Note: 'leveraged', 'utilised', 'enhanced', 'robust', 'impactful', 'proactive' are PERMITTED — use sparingly and naturally.)\n"
        '- No "I am passionate about" — show passion through a specific concrete example.\n'
        "- No transition openers (Furthermore, Moreover, Additionally) — lead every paragraph with the actual point.\n"
        "- Vary paragraph rhythm: mix short direct sentences with longer technical ones.\n"
        "- LANGUAGE: English only throughout. Translate all German JD terms to English before writing.\n\n"
        "Respond with valid JSON only. No markdown. No code fences.\n"
    )


_CV_SYSTEM = _build_cv_system()
_CL_SYSTEM = _build_cl_system()

# ── CV Generator ───────────────────────────────────────────────

_CV_PROMPT = """
TARGET JOB:
Title: {title}
Company: {company}
Location: {location}
Job Description:
{description}

Generate a fully ATS-optimised, humanised resume tailored 100% to the job description above.
Act as a 15+ year experienced ATS CV writer. The output must be practical, relatable, and undetectable as AI-written.

STRICT RULES — follow exactly, never do extra, never do less:
1. Professional Summary: exactly ~60 words, keyword-rich, role-aligned to the JD. HARD CAP: ≤ 65 words. Bold 2–3 JD keywords inline using **double asterisks**. Count before outputting.
2. Core Competencies: comma- or pipe-separated list of ALL JD tools, methodologies, and domain skills — no word cap. Include every relevant keyword from the JD. Bold every TOOL name inline using **double asterisks** (e.g. **Power BI**, **Python (Pandas)**, **SQL**, **SAP FI/CO**). Leave methodologies and domain terms unbold.
3. Each role: exactly 4 bullets. FORMAT: ONE complete sentence per bullet, action-verb led — no "Label:" prefix, no bold opener, no colon in the first 30 characters. Inline **bold** allowed ONLY for JD keywords mid-sentence.
   — Plain prose, written the way a real recruiter expects to read a CV.
   — HARD CAP per bullet: ≤ 30 words.
4. JD-keyword bold pulses: 1–2 per bullet maximum; bold a keyword only on its FIRST occurrence per section; total bold spans across the whole CV between 10 and 15 (never above 18).
5. ZERO-GAP ATS COVERAGE — every single keyword from the MANDATORY ATS list must appear verbatim
   in the final CV. No exceptions. Priority order:
     a) Core skill → embed naturally in a bullet or summary.
     b) Adjacent tool with limited exposure → add to Core Competencies AND include a phrase in one
        bullet ("supported workflows involving [tool]", "gained exposure to [tool] during X").
     c) Methodology/domain with no direct ownership → list in Core Competencies alongside the closest
        real experience (e.g. "Agile Reporting" if you used sprint boards).
   Leaving any MANDATORY keyword uncovered is a hard failure — find a plausible home for every one.
6. Metrics: use a metric wherever it makes the bullet stronger and stays realistic for this profile (5–30% range, minutes/hours saved, thousands of records).
   Do not force a number into every bullet — concrete qualitative outcomes are equally strong.
   Keep all values believable for 2–3 years experience in ops/analytics at Accenture + Chintamani level.
7. Both project descriptions: ONE sentence only, max 20 words each. Tailored to JD. Bold the dominant tool inline (e.g. **Power BI**, **Python (Pandas)**). No paragraph, no multiple sentences.
8. Content must be practical, relatable, and not detectable as AI-written.
9. Vary bullet length naturally — at least one short (≤15 words) and one detailed (25-30 words) per role.
   No two consecutive bullets start with the same verb. No named pattern rotation.
10. Cover: data analysis, business insight, operations, reporting, stakeholder impact — distributed naturally.
11. Write the way a strong human writer would — let the content decide the structure, not a template.

Respond with this exact JSON schema (no extra keys, no missing keys):
{{
  "summary": "<~60 word professional summary tailored to JD, with 2–3 inline **bold** JD keywords>",
  "competencies": "<All JD tools and skills, no word cap. Bold every tool with **double asterisks**. Example: '**Power BI** · **Python (Pandas)** · **SQL** · **Power Query** · **VBA** · **SAP FI/CO** · **Tableau** · Financial Reporting · Variance Analysis · KPI Dashboards · Data Governance · ETL · Data Modeling · SLA Management'>",
  "chintamani": [
    "One natural sentence led by a strong action verb, with 1–2 inline **bold** JD keywords, tailored to this JD.",
    "One natural sentence with a concrete qualitative outcome — specific, not vague. Inline **bold** only for the dominant JD tool/method.",
    "One natural sentence carrying a metric that genuinely fits the base-CV achievements. 0–1 inline **bold** keyword.",
    "One natural sentence on operational or stakeholder impact. 0–1 inline **bold** keyword."
  ],
  "accenture": [
    "One natural sentence led by a strong action verb, with 1–2 inline **bold** JD keywords, tailored to this JD.",
    "One natural sentence carrying a believable metric from the base CV. 0–1 inline **bold** keyword.",
    "One natural sentence on analysis or insight that drove a real decision. 0–1 inline **bold** keyword.",
    "One natural sentence on a process or reporting improvement. 0–1 inline **bold** keyword."
  ],
  "project1_desc": "<ONE sentence ≤ 20 words: Supplier Spend Analytics project tailored to JD. Bold the dominant tool, e.g. **Power BI**.>",
  "project2_desc": "<ONE sentence ≤ 20 words: Insurance Operations Reporting Automation tailored to JD. Bold the dominant tool, e.g. **Python (Pandas)**.>"
}}
"""

# ── CL Generator ──────────────────────────────────────────────

_CL_PROMPT = """
TARGET JOB:
Title: {title}
Company: {company}
Location: {location}
Job Description (first 2500 chars):
{description}

Candidate notes for this application: {notes}

Please create a cover letter for the position above according to the given job description, addressing ALL of the following points exactly — never do extra, never do less:

PARAGRAPH 1 — STORY-FIRST OPENING (~80 words):
The first sentence MUST be a concrete moment from YOUR work — a number, a discovery, a fix you made. Not a description of the company. Not "X sits at the intersection of...". Not "I am writing/excited/thrilled". Pick the anecdote whose details most clearly map to THIS JD's stated needs (procurement, automation, PMO, reporting, governance, stakeholder mgmt). After that opening sentence, connect it in 2–3 sentences to why this specific role and this specific team fits. Vary the lead story across applications — do not anchor every cover letter on the same metric.

PARAGRAPH 2 — EXPERIENCE MAPPED TO JD (~100 words):
Explain how your experience fits the role. Reference your time at **Accenture Solutions** and **Chintamani Thermal Technologies** — show how skills developed at each directly match JD requirements. Demonstrate Power BI, Python, SQL, Excel, SAP with specific examples. Include at least 2 quantified metrics. The story you used in Para 1 is OFF LIMITS — use different angles here.

PARAGRAPH 3 — ONE PROJECT, DEEP (~70 words):
Pick the ONE project most aligned to the JD and go deep on it (~55 words: what was built, method, what changed). Then close with a 1-sentence nod (~15 words) to the second project as supporting evidence.
  • JD about reporting / automation / Python / SQL → lead with **Insurance Operations Reporting Automation**.
  • JD about procurement / cost / PMO / governance / dashboards → lead with **Supplier Spend Analytics and Cost Dashboard**.
Both names appear in **bold**, but only one is deep. Never end this paragraph with a dangling article ("The ", "A ").

PARAGRAPH 4 — CONTRIBUTION (~60 words):
Describe what you will contribute if selected. Emphasise data-driven decision-making and cross-functional collaboration. Frame your skills as direct solutions to the company's operational and analytical needs. If you mention the MSc, frame it as "where I'm formalising what I've practiced for the past 3 years" — NOT "what has prepared me" (the MSc started March 2026 and is too new for that claim).

PARAGRAPH 5 — CONFIDENT CLOSE (~50 words):
Express genuine interest in the opportunity. Mention availability for Werkstudent hours (20 hrs/week) and relocation readiness if relevant. GERMAN HANDLING: if the JD is in German OR the location is in Bavaria/Munich/Austria, include ONE factual sentence about German — "currently at A2 and actively progressing through daily exposure in Ingolstadt". End with a SPECIFIC call-to-action — e.g. "I would welcome a 20-minute conversation about how I can support [team] this semester" — not a generic "I look forward to hearing from you".

ADDRESS RULE (zero hallucination):
- If the JOB DESCRIPTION explicitly lists a street + postcode + city for the company, copy it VERBATIM into company_addr.
- If the JD lists only a city (e.g. "Munich"), use "<Company legal name>, <City>, Germany" — city only, NO street or postcode.
- If no city is given either, use "<Company legal name>, Germany".
- NEVER invent a street name, building number, or postal code. Made-up addresses go to real recruiters with the candidate's name on them.

RULES:
- Sound human, confident, and natural. Must NOT be detectable as AI-written.
- Use **double asterisks** to bold company and project names only.
- Never use: leveraged, utilised, cutting-edge, delve, foster, garner, showcase, pivotal, crucial, enhance, "serves as", "boasts", "I am passionate about", "I am excited to"
- No "I am passionate about" — show passion through a concrete specific, never state it.
- No transition openers (Furthermore, Moreover, Additionally).
- PAGE LIMIT: Maximum 1 page total. Keep every paragraph tight.
- LANGUAGE: English only. Translate all German JD terms to English.

FEASIBILITY LAW (every claim must be defensible in interview):
- Chintamani = Mar 2025–Feb 2026 (Assistant Mgr level); Accenture = Nov 2022–Feb 2025 (New Associate level).
- ZERO TOLERANCE: ChatGPT/Claude/Gemini/Copilot/LLM/RAG/AI-Governance/Prompt-Engineering/internal-AI-tool claims belong to Chintamani ONLY — never Accenture, not even as "exposure" or "research on" or "documentation of".
- Accenture verbs: supported / contributed to / assisted with / analysed. Chintamani verbs: coordinated / built / delivered / presented.
- Numbers in the 5–30% / minutes-hours / thousands-of-records / 3–6 categories range — never millions of records or company-wide rollouts.
- Primary tools (Power BI, Python/Pandas, SQL, Excel VBA/Power Query, SAP FI/CO, Power Automate, Tableau, MS365) may be claimed directly. For adjacent JD tools (Azure, Looker, R, JIRA, Qlik, Snowflake, dbt, etc.), create a brief plausible exposure mention — never claim architectural ownership, only contribution/exposure language.

Respond with this exact JSON schema (no extra keys, no missing keys):
{{
  "company_name": "<Company name + role for the address block, e.g. 'Allianz SE – Werkstudent Business Analytics'>",
  "company_addr": "<Company address — see ADDRESS RULE below. NEVER fabricate a street/postcode.>",
  "subject_line": "<Subject line, e.g. 'Application – [Role Title] | [Job ID if known]'>",
  "para1": "<Para 1 — ~80 words, why this company + role, do NOT start with 'I am writing to apply' or 'I am excited'>",
  "para2": "<Para 2 — ~100 words, experience at **Accenture Solutions** and **Chintamani Thermal Technologies** mapped to JD, ≥2 metrics>",
  "para3": "<Para 3 — ~70 words, **Supplier Spend Analytics and Cost Dashboard** + **Insurance Operations Reporting Automation**, specific achievements tied to JD>",
  "para4": "<Para 4 — ~60 words, what you will contribute, data-driven + cross-functional focus>",
  "para5": "<Para 5 — ~50 words, confident close, availability/relocation readiness, forward-looking>"
}}
"""



# Map keys → hardcoded defaults (built after the strings are defined above)
_DEFAULTS: dict = {
    "cv_system": _CV_SYSTEM,
    "cv_prompt": _CV_PROMPT,
    "cl_system": _CL_SYSTEM,
    "cl_prompt": _CL_PROMPT,
}


class CVGenerator:
    def __init__(self, tracker=None):
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tracker = tracker

    def _log_cost(self, job_id: str, call_type: str, response) -> None:
        if not self._tracker:
            return
        cost = calc_cost(
            config.CLAUDE_MODEL,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        self._tracker.log_api_cost(
            job_id, call_type, config.CLAUDE_MODEL,
            response.usage.input_tokens, response.usage.output_tokens, cost,
        )

    async def generate_cv_content(
        self, job: JobListing, feedback: str = "", jd_keywords: list | None = None, jd_focus: str = ""
    ) -> Dict:
        prompt = get_prompt("cv_prompt").format(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:5000] if job.description else "Not provided.",
        )
        if jd_focus:
            # Strategic brief goes FIRST — Claude reads the writing brief before the JD
            prompt = f"{jd_focus}\n\n" + prompt
        if jd_keywords:
            kw_block = (
                f"\n\n{'='*50}\n"
                "MANDATORY ATS KEYWORDS — every item below MUST appear verbatim (exact spelling/casing) in the CV.\n"
                "Zero gaps allowed. For each keyword:\n"
                "  • If it is a skill you own → embed naturally in a bullet, summary, or Core Competencies.\n"
                "  • If it is a tool you have limited exposure to → add to Core Competencies AND place a\n"
                "    brief qualifying phrase in one bullet ('supported workflows involving X', 'gained exposure\n"
                "    to X during Y project', 'contributed to X-tracked deliverables').\n"
                "  • If it is a methodology/domain term → list in Core Competencies and connect it to the\n"
                "    closest real experience you have.\n"
                "Do NOT skip any keyword because it feels like a stretch — create a plausible mention.\n"
                f"{chr(10).join(f'  • {k}' for k in jd_keywords)}\n"
                f"{'='*50}\n"
            )
            prompt = kw_block + "\n" + prompt
        if feedback:
            prompt += (
                f"\n\n{'='*50}\n"
                "QUALITY REPORT FROM PREVIOUS ATTEMPT — FIX EVERY ISSUE BEFORE OUTPUTTING:\n"
                f"{feedback}\n"
                f"{'='*50}\n"
            )

        logger.info(f"Generating CV content for {job.title} @ {job.company}")
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=config.CLAUDE_MODEL,
            max_tokens=4500,
            system=[{"type": "text", "text": get_prompt("cv_system"), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job.job_id, "cv", response)

        if response.stop_reason == "max_tokens":
            logger.warning(
                f"CV generation hit max_tokens for {job.title} @ {job.company} — "
                "response may be truncated; will retry."
            )

        raw_text = response.content[0].text if response.content else ""
        if not raw_text.strip():
            logger.warning(
                f"CV generation returned empty content for {job.title} @ {job.company} "
                f"(stop_reason={response.stop_reason!r}) — will retry."
            )
            raise ValueError(f"Empty CV response from Claude (stop_reason={response.stop_reason!r})")

        try:
            raw = self._clean_json(raw_text)
        except Exception as exc:
            logger.warning(f"CV JSON parse failed: {exc} | Raw (first 300 chars): {raw_text[:300]!r}")
            raise

        data = json.loads(raw)
        logger.info(f"CV generated — {len(data)} sections ready")
        return data

    async def generate_cl_content(
        self, job: JobListing, application_notes: str = "", feedback: str = "",
        jd_keywords: list | None = None, cv_content: dict | None = None,
        company_fact: str = "", jd_focus: str = "",
    ) -> Dict:
        prompt = get_prompt("cl_prompt").format(
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:4000] if job.description else "Not provided.",
            notes=application_notes or "None",
        )
        if jd_focus:
            # Strategic brief goes FIRST — Claude reads the writing brief before the JD
            prompt = f"{jd_focus}\n\n" + prompt
        if company_fact:
            fact_block = (
                f"\n\n{'='*50}\n"
                "COMPANY ANCHOR FACT (from Wikipedia — verified, not invented):\n"
                f"  {company_fact}\n\n"
                "Use this fact to ground PARA 1 or PARA 4. Weave it naturally — never quote it\n"
                "verbatim, never use the words 'according to Wikipedia'. Pair it with what\n"
                "YOU bring: how your specific work maps to what they do. The goal is to prove\n"
                "you actually know who they are, not to recite their history.\n"
                "If the fact doesn't fit the JD theme, ignore it — do not force it in.\n"
                f"{'='*50}\n"
            )
            prompt = fact_block + "\n" + prompt
        if cv_content:
            bullets_chintamani = "\n".join(f"  - {b}" for b in cv_content.get("chintamani", []))
            bullets_accenture  = "\n".join(f"  - {b}" for b in cv_content.get("accenture",  []))
            cv_block = (
                f"\n\n{'='*50}\n"
                "CV BULLETS ALREADY WRITTEN — your cover letter must reference and expand on these.\n"
                "Do not copy them verbatim. Use the same achievements, angles, and metrics to tell\n"
                "a consistent story — the CL deepens what the CV states.\n\n"
                f"Chintamani Thermal Technologies:\n{bullets_chintamani}\n\n"
                f"Accenture Solutions:\n{bullets_accenture}\n"
                f"{'='*50}\n"
            )
            prompt = cv_block + "\n" + prompt
        if jd_keywords:
            kw_block = (
                f"\n\n{'='*50}\n"
                "MANDATORY ATS KEYWORDS — every keyword below must appear verbatim (exact spelling/casing).\n"
                "For core skills: weave naturally into experience descriptions.\n"
                "For adjacent tools not in your primary toolkit: include a brief exposure phrase\n"
                "  ('gained experience with X', 'contributed to workflows involving X').\n"
                "Do NOT skip any keyword — zero gaps allowed.\n"
                f"{chr(10).join(f'  • {k}' for k in jd_keywords)}\n"
                f"{'='*50}\n"
            )
            prompt = kw_block + "\n" + prompt
        if feedback:
            prompt += (
                f"\n\n{'='*50}\n"
                "QUALITY REPORT FROM PREVIOUS ATTEMPT — FIX EVERY ISSUE BEFORE OUTPUTTING:\n"
                f"{feedback}\n"
                f"{'='*50}\n"
            )

        logger.info(f"Generating CL content for {job.title} @ {job.company}")
        response = await asyncio.to_thread(
            self.client.messages.create,
            model=config.CLAUDE_MODEL,
            max_tokens=2500,
            system=[{"type": "text", "text": get_prompt("cl_system"), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job.job_id, "cl", response)

        if response.stop_reason == "max_tokens":
            logger.warning(
                f"CL generation hit max_tokens for {job.title} @ {job.company} — "
                "response may be truncated; will retry."
            )

        raw_text = response.content[0].text if response.content else ""
        if not raw_text.strip():
            logger.warning(
                f"CL generation returned empty content for {job.title} @ {job.company} "
                f"(stop_reason={response.stop_reason!r}) — will retry."
            )
            raise ValueError(f"Empty CL response from Claude (stop_reason={response.stop_reason!r})")

        try:
            raw = self._clean_json(raw_text)
        except Exception as exc:
            logger.warning(f"CL JSON parse failed: {exc} | Raw (first 300 chars): {raw_text[:300]!r}")
            raise

        data = json.loads(raw)
        logger.info(f"CL generated — {len(data)} sections ready")
        return data

    @staticmethod
    def _clean_json(text: str) -> str:
        import json as _json
        text = text.strip()

        if not text:
            raise ValueError("Claude returned an empty response — cannot parse JSON.")

        # Strip markdown code fences
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        # Skip any prose before the first JSON object/array
        for bracket in ("{", "["):
            idx = text.find(bracket)
            if idx != -1:
                text = text[idx:]
                break

        text = text.strip()

        if not text:
            raise ValueError("No JSON object found in Claude's response.")

        # raw_decode extracts exactly the first valid JSON object, ignoring any
        # trailing text or second object Claude may have appended on retries.
        obj, _ = _json.JSONDecoder().raw_decode(text)   # raises JSONDecodeError if still invalid
        return _json.dumps(obj, ensure_ascii=False)
