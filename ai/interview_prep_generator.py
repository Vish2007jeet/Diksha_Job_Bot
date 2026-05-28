"""
Interview Prep Generator
========================
Produces a self-contained, Sudarshan-style HTML interview prep guide.

Design
------
- FOUR Haiku calls (each kept well under the 8000-token output limit):
    Call 1: Part 1 HR questions (20 QAs) + Part 5 Ask Them (7 questions)
    Call 2: Part 2 STAR Behavioural (16 QAs)
    Call 3: Part 3 Technical Domain (5 QAs) + Part 4 CV Defence (4 QAs)
    Call 4: Part 7 CV Bullet-Point STAR Defence (18 bullets across all roles)
- 7 sections — adds Part 3 Technical per JD + Part 7 CV STAR per bullet
- Full question counts — no trimming

Output: Interview_Prep_<suffix>.html saved in application folder.
"""
from __future__ import annotations

import html as _html
import json
from pathlib import Path
from typing import Optional

import anthropic

import config
from utils.cost import calc_cost
from utils.logger import logger
from utils.models import JobListing

# ── Model override for interview prep (Haiku — cheaper, still excellent) ──────
_IP_MODEL = "claude-haiku-4-5-20251001"

# ── CSS (identical to Sudarshan template) ─────────────────────────────────────
_CSS = """
  :root {
    --primary: #1e3a5f; --primary-light: #2d5585;
    --accent: #c8956d; --accent-dark: #a87850;
    --bg: #fafaf7; --card: #ffffff;
    --text: #1a1a1a; --text-light: #555; --text-muted: #888;
    --border: #e5e5e0; --q-bg: #f4f1ea; --tip-bg: #eaf2f6;
    --shadow: 0 1px 3px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.03);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; scroll-padding-top: 70px; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.65;
    font-size: 16px; -webkit-font-smoothing: antialiased;
  }
  nav.topnav {
    position: sticky; top: 0; z-index: 100; background: var(--primary);
    color: white; padding: 12px 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.15);
    display: flex; gap: 8px; overflow-x: auto; white-space: nowrap; scrollbar-width: thin;
  }
  nav.topnav a {
    color: rgba(255,255,255,0.85); text-decoration: none; padding: 6px 12px;
    border-radius: 6px; font-size: 14px; font-weight: 500; transition: all 0.15s; flex-shrink: 0;
  }
  nav.topnav a:hover { background: rgba(255,255,255,0.12); color: white; }
  nav.topnav .brand {
    font-weight: 700; color: white; padding-right: 16px;
    border-right: 1px solid rgba(255,255,255,0.2); margin-right: 4px;
  }
  .container { max-width: 880px; margin: 0 auto; padding: 32px 24px 80px; }
  header.hero {
    text-align: center; padding: 40px 20px 32px;
    border-bottom: 2px solid var(--border); margin-bottom: 32px;
  }
  header.hero h1 { font-size: 2.2rem; color: var(--primary); margin-bottom: 8px; letter-spacing: -0.5px; }
  header.hero .subtitle { color: var(--text-light); font-size: 1.05rem; }
  header.hero .meta {
    margin-top: 16px; display: inline-flex; gap: 12px; flex-wrap: wrap; justify-content: center;
  }
  header.hero .meta span {
    background: var(--q-bg); padding: 4px 12px; border-radius: 20px;
    font-size: 0.85rem; color: var(--text-light);
  }
  .controls { display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap; justify-content: center; }
  .controls button {
    background: var(--primary); color: white; border: none; padding: 10px 18px;
    border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer;
    transition: all 0.15s; font-family: inherit;
  }
  .controls button:hover { background: var(--primary-light); }
  .controls button.secondary {
    background: white; color: var(--primary); border: 1.5px solid var(--primary);
  }
  .controls button.secondary:hover { background: var(--q-bg); }
  section.part { margin-bottom: 48px; }
  section.part > h2 {
    font-size: 1.6rem; color: var(--primary); margin-bottom: 6px;
    padding-bottom: 10px; border-bottom: 3px solid var(--accent); display: inline-block;
  }
  section.part > .part-intro {
    color: var(--text-light); margin-bottom: 24px; font-style: italic; font-size: 0.95rem;
  }
  section.subsection { margin-top: 28px; margin-bottom: 20px; }
  section.subsection h3 {
    font-size: 1.15rem; color: var(--primary-light); margin-bottom: 14px;
    padding-left: 12px; border-left: 4px solid var(--accent);
  }
  details.qa {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    margin-bottom: 12px; box-shadow: var(--shadow); transition: all 0.2s; overflow: hidden;
  }
  details.qa:hover { border-color: var(--accent); }
  details.qa[open] { border-color: var(--primary-light); }
  details.qa summary {
    padding: 16px 20px; cursor: pointer; font-weight: 600; color: var(--primary);
    list-style: none; display: flex; align-items: flex-start; gap: 12px; transition: background 0.15s;
  }
  details.qa summary::-webkit-details-marker { display: none; }
  details.qa summary:hover { background: var(--q-bg); }
  details.qa summary::before {
    content: "+"; display: inline-flex; align-items: center; justify-content: center;
    width: 24px; height: 24px; background: var(--accent); color: white; border-radius: 50%;
    font-size: 18px; font-weight: 300; flex-shrink: 0; transition: transform 0.2s; line-height: 1;
  }
  details.qa[open] summary::before { content: "\2212"; transform: rotate(180deg); }
  .q-number { color: var(--accent-dark); font-weight: 700; margin-right: 6px; }
  .answer {
    padding: 18px 20px 20px 56px; color: var(--text); border-top: 1px solid var(--border);
  }
  .answer p { margin-bottom: 12px; }
  .answer p:last-child { margin-bottom: 0; }
  .answer strong { color: var(--primary); }
  .answer .star-label {
    display: inline-block; background: var(--accent); color: white;
    padding: 1px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 700; margin-right: 4px;
  }
  .tip {
    background: var(--tip-bg); border-left: 4px solid var(--primary-light);
    padding: 12px 16px; margin: 16px 0; border-radius: 0 8px 8px 0; font-size: 0.95rem;
  }
  .tip strong { color: var(--primary); }
  .warning {
    background: #fff4e6; border-left: 4px solid var(--accent-dark);
    padding: 12px 16px; margin: 16px 0; border-radius: 0 8px 8px 0; font-size: 0.95rem;
  }
  .warning strong { color: var(--accent-dark); }
  .ask-list {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 20px 20px 20px 44px; box-shadow: var(--shadow); margin-bottom: 16px;
  }
  .ask-list li { margin-bottom: 10px; color: var(--text); }
  .ask-list li:last-child { margin-bottom: 0; }
  .tips-block {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 20px 24px; box-shadow: var(--shadow); margin-bottom: 16px;
  }
  .tips-block h4 { color: var(--primary); margin-bottom: 12px; font-size: 1.05rem; }
  .tips-block ul { padding-left: 20px; }
  .tips-block li { margin-bottom: 6px; }
  .intro-box { background: var(--q-bg); border-radius: 12px; padding: 20px 24px; margin-bottom: 32px; }
  .intro-box h3 { color: var(--primary); margin-bottom: 10px; font-size: 1.1rem; }
  .intro-box ul { padding-left: 20px; }
  .intro-box li { margin-bottom: 6px; color: var(--text); }
  footer {
    text-align: center; padding: 40px 20px;
    border-top: 2px solid var(--border); margin-top: 48px; color: var(--text-light);
  }
  footer .good-luck { font-size: 1.3rem; color: var(--primary); font-weight: 600; margin-bottom: 8px; }
  @media (max-width: 640px) {
    body { font-size: 15px; }
    .container { padding: 20px 14px 60px; }
    header.hero h1 { font-size: 1.6rem; }
    section.part > h2 { font-size: 1.35rem; }
    details.qa summary { padding: 14px; font-size: 0.95rem; }
    .answer { padding: 14px 14px 18px 14px; }
    nav.topnav { padding: 10px 12px; }
  }
  @media print {
    nav.topnav, .controls { display: none; }
    details.qa { page-break-inside: avoid; border: 1px solid #ccc; }
    details.qa summary { background: #f0f0f0; }
    body { background: white; }
    .container { max-width: 100%; padding: 0; }
  }
"""

# ── Shared candidate background (loaded from user_config.yaml) ────────────────
_CANDIDATE_CONTEXT = config.INTERVIEW_PROFILE_TEXT

_BEHAVIOUR_RULES = """
RULES (follow exactly):
- All answers must sound natural, confident, and human — not AI-generated.
- Use the candidate's real experiences — cite specific projects, tools, and companies by name.
- Tailor every answer to the specific job description provided.
- STAR format: each field (S/T/A/R) should be 2-4 full sentences. Rich, specific, believable.
- HR answers: 3-6 sentences per paragraph. No bullet points in the answer text.
- Include tips/warnings where genuinely useful (1-2 sentences max).
- Respond with VALID JSON only. No markdown, no code fences, no trailing commas.
"""

# ── CALL 1 PROMPT: HR Questions (Part 1) + Ask Them (Part 5) ──────────────────
_PROMPT_HR = """
TARGET JOB:
Title: {title}
Company: {company}
Location: {location}
Job Description:
{description}

Generate Part 1 (HR questions) and Part 5 (questions to ask the interviewer) for this specific role.

Return EXACTLY this JSON (all keys required, no extras):

{{
  "interview_type": "<e.g. HR + Technical Interview / HR Screening / Assessment Centre>",
  "meta_tags": ["<tag1: company type>", "<tag2: role type>", "<tag3: e.g. STAR Method>"],

  "p1_opening": [
    {{"q": "Tell me about yourself.", "a": ["<paragraph 1 — ~80 words, introduce the candidate, tailor to this role>", "<paragraph 2 — ~60 words, current situation and why this role>"]}},
    {{"q": "Walk me through your CV.", "a": ["<clear chronological summary, highlight what is most relevant to this JD>"]}},
    {{"q": "Why did you choose your current university and program?", "a": ["<genuine, confident answer — program strengths, industry connections, career goals>"]}},
    {{"q": "What drew you to your Master's specialisation?", "a": ["<specific: curriculum relevance, industry connections, extracurricular access>"]}}
  ],

  "p1_transitions": [
    {{"q": "How does your current work experience connect to this role?", "a": ["<link skills from current/recent role to this role's requirements>"]}},
    {{"q": "How do your extracurricular projects apply to this position?", "a": ["<specific engineering work done, map to JD requirements>"]}},
    {{"q": "How does your technical background connect to what this role requires?", "a": ["<clear bridge between candidate experience and role needs>"]}},
    {{"q": "What is your career plan after finishing your studies?", "a": ["<timeline, thesis/graduation plan, transition to full-time>"]}}
  ],

  "p1_fit": [
    {{"q": "Why {company}?", "a": ["<paragraph 1 — specific reasons tailored to the company: products, culture, reputation>", "<paragraph 2 — how his background matches their needs>"], "tip": "<research tip: 2 specific facts to find before the interview>"}},
    {{"q": "Why should we hire you over other candidates?", "a": ["<3 strong differentiators: Formula Student hands-on, Infineon simulation experience, cross-cultural adaptability>",  "<confident close>"], "tip": null}},
    {{"q": "Where do you see yourself in 3-5 years?", "a": ["<senior engineering or technical lead role, Germany-based, language growth>"]}},
    {{"q": "What do you know about our company?", "a": ["<3-4 concrete facts about {company}: products, recent news, culture>"], "tip": "Spend 30 minutes on their website, LinkedIn, and Glassdoor before the interview."}}
  ],

  "p1_practical": [
    {{"q": "What is your visa and work permit situation in Germany?", "a": ["<student permit now, Blue Card eligible after graduation, no sponsorship needed for Praktikum/Werkstudent>"]}},
    {{"q": "When can you start?", "a": ["<flexible answer — thesis timeline, possible immediate start for Werkstudent/Praktikum>"]}},
    {{"q": "What are your salary expectations?", "a": ["<role-appropriate range in Euros — Werkstudent vs Praktikum vs full-time>"], "warning": "<realistic salary range for this role type in Bavaria>"}},
    {{"q": "Your German is A2 — how will you manage in a German work environment?", "a": ["<honest acknowledgment + active steps + English as bridge + commitment to B1 by specific date>"]}},
    {{"q": "Are you open to relocation within Germany?", "a": ["<yes, fully flexible — already relocated from India, comfortable with change>"]}}
  ],

  "p1_self": [
    {{"q": "What are your three main strengths?", "a": ["<strength 1 — simulation/analytical with Infineon example>", "<strength 2 — hands-on engineering with Formula Student example>", "<strength 3 — adaptability/cross-cultural>"]}},
    {{"q": "What is your main weakness or area for improvement?", "a": ["<German language — honest, with concrete plan. Second: tendency to over-engineer solutions — learning to balance.>"]}},
    {{"q": "Describe your ideal work environment.", "a": ["<structured, collaborative, hands-on, international team — maps to automotive engineering culture>"]}}
  ],

  "p5_ask_them": [
    "<role-specific thoughtful question 1>",
    "<question 2 about day-to-day tools/methods used in the role>",
    "<question 3 about team structure and who he would work with>",
    "<question 4 about learning and development opportunities>",
    "<question 5 about biggest challenge the team faces right now>",
    "<question 6 about what success looks like in the first 3 months>",
    "<question 7 about next steps in the hiring process>"
  ],

  "good_luck_msg": "<personalized, warm, specific good luck message for the candidate for this role at {company}>"
}}
"""

# ── CALL 2 PROMPT: STAR Behavioural only (16 QAs) ─────────────────────────────
_PROMPT_STAR = """
TARGET JOB:
Title: {title}
Company: {company}
Location: {location}
Job Description:
{description}

Generate Part 2 — STAR Behavioural questions for this role.

STAR FORMAT: For each question provide intro (context project name), then S/T/A/R fields.
Each S/T/A/R field must be 2-4 full sentences. Use the candidate's real projects.

Return EXACTLY this JSON (all keys required, no extras):

{{
  "p2_problem_solving": [
    {{"q": "Tell me about a complex technical problem you solved.", "intro": "<which project>", "s": "<2-3 sentences: context and the problem>", "t": "<1-2 sentences: the candidate's specific responsibility>", "a": "<3-4 sentences: exactly what was done, tools used, decisions made>", "r": "<2 sentences: outcome with specific metric>"}},
    {{"q": "Describe a time you used data or simulation to drive a technical decision.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "Give an example of identifying a technical risk or problem that others had missed.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}}
  ],

  "p2_initiative": [
    {{"q": "Tell me about a time you took initiative beyond your assigned responsibilities.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "Describe a process or workflow you improved without being asked.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}}
  ],

  "p2_teamwork": [
    {{"q": "Describe a project where you had to collaborate across different teams or functions.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "Tell me about working with a difficult colleague or resolving a disagreement.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "A time you disagreed with a supervisor or senior engineer — what did you do?", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}}
  ],

  "p2_adaptability": [
    {{"q": "Tell me about the biggest change you had to adapt to — professionally or personally.", "intro": "<Moving to Germany / starting at Infineon>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "Tell me about a project that failed or didn't go as planned. What did you learn?", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "How do you handle ambiguity when requirements are unclear or keep changing?", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}}
  ],

  "p2_communication": [
    {{"q": "Tell me about explaining a complex technical concept to a non-technical person.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "Describe working and communicating in an international or multicultural team.", "intro": "<Infineon / Formula Student / TH Ingolstadt>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}}
  ],

  "p2_pressure": [
    {{"q": "Tell me about managing multiple deadlines or priorities at the same time.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}},
    {{"q": "Describe a decision you had to make quickly with incomplete information.", "intro": "<project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}}
  ],

  "p2_leadership": [
    {{"q": "Tell me about a time you led a project or team without having a formal leadership title.", "intro": "<Formula Student or Magna project>", "s": "<>", "t": "<>", "a": "<>", "r": "<>"}}
  ]
}}
"""

# ── CALL 3 PROMPT: Technical Domain + CV Defence (9 QAs) ──────────────────────
_PROMPT_TECH = """
TARGET JOB:
Title: {title}
Company: {company}
Location: {location}
Job Description:
{description}

Generate Part 3 (technical domain questions specific to this JD) and Part 4 (CV defence questions) for this role.

For Part 3: all questions must be grounded in tools, methods, and concepts explicitly mentioned in the JD above.
For Part 4: challenge specific metrics and tools from the candidate's CV, force them to defend their numbers.

Return EXACTLY this JSON (all keys required, no extras):

{{
  "p3_technical": [
    {{"q": "<JD-specific technical question about the primary tool — tailor to what the candidate listed>", "a": "<detailed answer from the candidate's real experience — what they actually did, parameters, outputs>"}},
    {{"q": "<Ask the candidate to explain a core engineering concept from the JD>", "a": "<clear technical explanation + link to their project experience>"}},
    {{"q": "<Ask about a specific methodology from the JD that the candidate has experience with>", "a": "<specific answer: which project, what parameters, what the study showed>"}},
    {{"q": "<Ask a validation/debugging question relevant to the JD tools>", "a": "<step-by-step answer from real experience>"}},
    {{"q": "<Ask about a deliverable they would produce in this role>", "a": "<specific: what sections, how results are presented, example from their experience>"}}
  ],

  "p4_cv_defence": [
    {{"q": "<Pick a specific quantified result from the CV relevant to this role — ask how it was measured>", "a": "<honest, detailed explanation — baseline, tool used, comparison method>"}},
    {{"q": "<Ask about a specific tool listed on the CV that also appears in the JD>", "a": "<specific project, setup details, output format>"}},
    {{"q": "<Challenge a bullet on the CV — ask for the baseline value and how it was measured>", "a": "<>"}},
    {{"q": "<Ask about an extracurricular or project experience — what exactly was designed, built, or tested?>", "a": "<specific contribution: component, design decisions, test results, lessons learned>"}}
  ]
}}
"""

# ── CALL 4 PROMPT: CV Bullet-Point STAR Defence ──────────────────────────────
def _build_cv_star_prompt() -> str:
    return (
        "TARGET ROLE: {title} at {company}\n\n"
        "Generate Part 7 — a STAR-format answer for EVERY bullet point on the candidate's CV.\n"
        'These are used when an interviewer asks "Tell me about this specific achievement" or\n'
        '"Explain how you achieved that number."\n\n'
        "CRITICAL RULE: The Action field MUST stay faithful to the exact wording on the CV.\n"
        "Do NOT invent steps, tools, or decisions not mentioned in the CV bullet text below.\n"
        "Only expand on what is explicitly stated. Every metric in Result must match the CV exactly.\n\n"
        "STAR FORMAT per bullet:\n"
        "  bullet: 1-line summary (role + achievement)\n"
        "  intro: 1 sentence — employer, role, context\n"
        "  s: Situation — 2 sentences (the problem or environment)\n"
        "  t: Task — 1-2 sentences (the candidate's specific responsibility)\n"
        "  a: Action — 2-3 sentences (expand ONLY on what the CV bullet describes; same tools, same verbs)\n"
        "  r: Result — 1-2 sentences (exact CV metric + broader impact for {{company}})\n\n"
        "All answers must sound natural, confident, and human — NOT AI-generated.\n\n"
        f"CV BULLET TEXT (use as source of truth for Action field):\n\n{config.CV_BULLETS_TEXT}\n\n"
        "Return EXACTLY this JSON (no markdown, no code fences, no trailing commas):\n\n"
        "{{\n"
        '  "cv_role1": [{{"bullet": "<summary>", "intro": "", "s": "", "t": "", "a": "", "r": ""}}, ...],\n'
        '  "cv_role2": [{{"bullet": "<summary>", "intro": "", "s": "", "t": "", "a": "", "r": ""}}, ...],\n'
        '  "cv_role3": [{{"bullet": "<summary>", "intro": "", "s": "", "t": "", "a": "", "r": ""}}, ...],\n'
        '  "cv_role4": [{{"bullet": "<summary>", "intro": "", "s": "", "t": "", "a": "", "r": ""}}, ...],\n'
        '  "cv_extra": [{{"bullet": "<summary>", "intro": "", "s": "", "t": "", "a": "", "r": ""}}, ...]\n'
        "}}\n"
    )

_PROMPT_CV_STAR = _build_cv_star_prompt()

# ── Part 0: Email Questions prompt ─────────────────────────────────────────────

_PROMPT_EMAIL_QUESTIONS = """\
You are an interview coach. The candidate received an interview invitation email that \
contains explicit questions the company wants them to address or prepare.

Extract every explicit question from the email and write a strong, tailored answer \
for each one. Questions may be stated directly ("Please tell us about..."), \
as bullet points, or as numbered items.

CANDIDATE PROFILE:
{profile}

JOB: {title} at {company}

INTERVIEW INVITATION EMAIL:
{email_body}

Rules:
- Extract ONLY questions that are explicitly stated in the email — do not invent questions.
- If the email contains NO explicit questions, return {{"questions": []}} immediately.
- Each answer must be specific, concrete, and reference Diksha's actual experience.
- Answers should be 3–5 sentences — detailed enough to speak from, short enough to remember.
- Sound confident and human. No AI filler phrases.

Return EXACTLY this JSON (no markdown, no code fences):
{{
  "questions": [
    {{
      "q": "<exact question as stated in the email>",
      "a": "<tailored 3–5 sentence answer grounded in her real experience>"
    }}
  ]
}}
"""

# ── HTML helpers ───────────────────────────────────────────────────────────────

def _e(text: str) -> str:
    return _html.escape(str(text), quote=False)

def _paras(items) -> str:
    if isinstance(items, str):
        items = [items]
    return "".join(f"<p>{_e(p)}</p>\n" for p in items if p)

def _star_block(q: dict) -> str:
    out = ""
    if q.get("intro"):
        out += f"<p><em>{_e(q['intro'])}</em></p>\n"
    for label, key in [("S", "s"), ("T", "t"), ("A", "a"), ("R", "r")]:
        val = q.get(key, "")
        if val:
            out += f'<p><span class="star-label">{label}</span> {_e(val)}</p>\n'
    return out

def _qa(q: dict, num: int, star: bool = False) -> str:
    body = _star_block(q) if star else _paras(q.get("a", []))
    extras = ""
    if q.get("tip"):
        extras += f'<div class="tip"><strong>Tip:</strong> {_e(q["tip"])}</div>\n'
    if q.get("warning"):
        extras += f'<div class="warning"><strong>Note:</strong> {_e(q["warning"])}</div>\n'
    return (
        f'\n<details class="qa">\n'
        f'  <summary><span><span class="q-number">Q{num}.</span> {_e(q.get("q",""))}</span></summary>\n'
        f'  <div class="answer">\n{body}{extras}  </div>\n</details>\n'
    )

def _subsection(heading: str, items: list, start: int, star: bool = False):
    html = f'<section class="subsection">\n<h3>{heading}</h3>\n'
    n = start
    for item in items:
        html += _qa(item, n, star=star)
        n += 1
    html += "</section>\n"
    return html, n

def _cv_star_qa(item: dict, num: int) -> str:
    """Render one CV bullet-point as a STAR-format collapsible QA card."""
    body = ""
    if item.get("intro"):
        body += f"<p><em>{_e(item['intro'])}</em></p>\n"
    for label, key in [("S", "s"), ("T", "t"), ("A", "a"), ("R", "r")]:
        val = item.get(key, "")
        if val:
            body += f'<p><span class="star-label">{label}</span> {_e(val)}</p>\n'
    bullet_text = item.get("bullet", f"CV Point {num}")
    return (
        f'\n<details class="qa">\n'
        f'  <summary><span><span class="q-number">#{num}.</span> {_e(bullet_text)}</span></summary>\n'
        f'  <div class="answer">\n{body}  </div>\n</details>\n'
    )

# ── HTML renderer ──────────────────────────────────────────────────────────────

def _render(hr: dict, star: dict, cv_star: dict, job: JobListing, email_questions: list | None = None) -> str:
    title    = f"Interview Prep — {job.title} @ {job.company}"
    subtitle = f"{config.USER_FULL_NAME} — {hr.get('interview_type', 'Interview')}"
    meta_html = "".join(f"<span>{_e(t)}</span>" for t in hr.get("meta_tags", ["STAR Method"]))

    q = 1

    # ── Part 1: HR ────────────────────────────────────────────────
    p1 = ""
    for heading, key in [
        ("1.1 Opening &amp; Background",      "p1_opening"),
        ("1.2 Career &amp; Role Journey",     "p1_transitions"),
        ("1.3 Role &amp; Company Fit",        "p1_fit"),
        ("1.4 Practical &amp; Logistics",     "p1_practical"),
        ("1.5 Self-Assessment",               "p1_self"),
    ]:
        block, q = _subsection(heading, hr.get(key, []), q, star=False)
        p1 += block

    # ── Part 2: STAR ─────────────────────────────────────────────
    p2 = ""
    for heading, key in [
        ("2.1 Problem-Solving &amp; Analytical Thinking", "p2_problem_solving"),
        ("2.2 Initiative &amp; Ownership",                "p2_initiative"),
        ("2.3 Teamwork &amp; Collaboration",              "p2_teamwork"),
        ("2.4 Adaptability &amp; Resilience",             "p2_adaptability"),
        ("2.5 Communication &amp; Intercultural",         "p2_communication"),
        ("2.6 Pressure &amp; Prioritisation",             "p2_pressure"),
        ("2.7 Leadership Without Title",                  "p2_leadership"),
    ]:
        block, q = _subsection(heading, star.get(key, []), q, star=True)
        p2 += block

    # ── Part 3: Technical ─────────────────────────────────────────
    p3 = ""
    for item in star.get("p3_technical", []):
        p3 += _qa(item, q, star=False)
        q += 1

    # ── Part 4: CV Defence ────────────────────────────────────────
    p4 = ""
    for item in star.get("p4_cv_defence", []):
        p4 += _qa(item, q, star=False)
        q += 1

    # ── Part 5: Ask Them ──────────────────────────────────────────
    ask_li = "".join(f"<li>{_e(s)}</li>\n" for s in hr.get("p5_ask_them", []))

    # ── Part 7: CV Bullet-Point STAR Defence ──────────────────────
    p7 = ""
    _role_keys = ["cv_role1", "cv_role2", "cv_role3", "cv_role4", "cv_extra"]
    cv_sections = list(zip(config.CV_STAR_SECTION_LABELS, _role_keys))
    for heading, key in cv_sections:
        items = cv_star.get(key, [])
        if items:
            p7 += f'<section class="subsection">\n<h3>{heading}</h3>\n'
            for item in items:
                p7 += _cv_star_qa(item, q)
                q += 1
            p7 += "</section>\n"

    # ── Part 6: Tips (static — same format as Sudarshan) ─────────
    tips = f"""
<div class="tips-block">
  <h4>✅ Before the Interview</h4>
  <ul>
    <li>Research <strong>{_e(job.company)}</strong> for 30 minutes — website, LinkedIn, Glassdoor, recent news. Find 2 specific facts.</li>
    <li>Re-read your CV and prepare a 30-second story for every bullet point and every metric.</li>
    <li>Check camera, mic, and internet if it is a video call. Test 10 minutes early.</li>
    <li>Have your CV and the job description open in a small window during the interview.</li>
    <li>Keep water, a notebook, and a pen nearby.</li>
    <li>Dress professionally — shirt minimum, even for video.</li>
  </ul>
</div>
<div class="tips-block">
  <h4>🎤 During the Interview</h4>
  <ul>
    <li>Smile when you say hello — first impressions count on video too.</li>
    <li>Speak slowly and clearly. It is fine to pause 2–3 seconds before answering.</li>
    <li>Use STAR for every behavioural question. Keep S+T to 20%, focus on A and R.</li>
    <li>If you do not understand a question, ask them to rephrase — better than a wrong answer.</li>
    <li>Keep answers to 1–2 minutes. Stop when you have made your point.</li>
    <li>Take short notes during the interview — it shows focus and engagement.</li>
    <li>Mention Formula Student when relevant — it is a strong differentiator in automotive.</li>
  </ul>
</div>
<div class="tips-block">
  <h4>🇩🇪 German Corporate Culture Tips</h4>
  <ul>
    <li>Address interviewers as <strong>Sie</strong> (formal) unless they explicitly invite <em>du</em>.</li>
    <li>Germans value precision and directness — back every claim with a specific fact, number, or project.</li>
    <li>Punctuality is non-negotiable — be online or at the door 5 minutes early.</li>
    <li>If your German comes up: acknowledge A2 honestly, state your improvement plan and your B1 target date. Show you are serious.</li>
    <li>Having a Blue Card path ready removes the most common HR concern for international candidates — state it proactively.</li>
    <li>German engineers respect technical depth. Do not oversimplify — go into detail when asked about your projects.</li>
  </ul>
</div>
<div class="tips-block">
  <h4>👋 At the End</h4>
  <ul>
    <li>Ask your 3–5 prepared questions from Part 5. Never say “I have no questions.”</li>
    <li>Thank the interviewers by name.</li>
    <li>Ask clearly: “What are the next steps and when can I expect to hear from you?”</li>
    <li>Within 24 hours, send a short thank-you email if you have their addresses.</li>
  </ul>
</div>
<div class="warning">
  <strong>Avoid in the first interview:</strong> salary negotiation details, vacation days, home-office frequency, or any requests before receiving an offer.
</div>
"""

    good_luck = _e(hr.get("good_luck_msg", f"Good luck at {job.company}!"))

    # ── Part 0: Email Questions (only if present) ──────────────────
    email_q_html = ""
    email_q_nav  = ""
    if email_questions:
        email_q_nav = '\n  <a href="#part0">0. Email Q&amp;A</a>'
        email_q_html = (
            '\n<!-- PART 0 -->\n'
            '<section class="part" id="part0" style="border-left: 4px solid var(--accent); padding-left: 16px;">\n'
            '  <h2>&#x1F4E7; Part 0 &mdash; Questions from Your Interview Invite</h2>\n'
            '  <p class="part-intro" style="color: var(--accent-dark); font-weight: 600;">'
            'The company explicitly asked these questions in their email. Prepare word-perfect answers &mdash; '
            'they WILL ask them.</p>\n'
        )
        for item in email_questions:
            email_q_html += (
                f'\n<details class="qa" open>\n'
                f'  <summary><span><span class="q-number" style="background:var(--accent);">★</span> '
                f'{_e(item.get("q", ""))}</span></summary>\n'
                f'  <div class="answer">\n<p>{_e(item.get("a", ""))}</p>\n  </div>\n</details>\n'
            )
        email_q_html += '</section>\n'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_e(title)}</title>
<style>{_CSS}</style>
</head>
<body>

<nav class="topnav">
  <a href="#top" class="brand">&#x1F4CB; Interview Prep</a>{email_q_nav}
  <a href="#part1">1. HR Questions</a>
  <a href="#part2">2. Behavioural STAR</a>
  <a href="#part3">3. Technical</a>
  <a href="#part4">4. CV Defence</a>
  <a href="#part5">5. Ask Them</a>
  <a href="#part6">6. Tips</a>
  <a href="#part7">7. CV STAR</a>
</nav>

<div class="container" id="top">

<header class="hero">
  <h1>Interview Preparation</h1>
  <p class="subtitle">{_e(subtitle)}</p>
  <div class="meta">
    <span>&#x1F3E2; {_e(job.company)}</span>
    {meta_html}
  </div>
</header>

<div class="intro-box">
  <h3>How to Use This Page</h3>
  <ul>
    <li><strong>Click any question</strong> to reveal the answer. Click again to hide it.</li>
    <li>Use <strong>Show All / Hide All</strong> below to test yourself.</li>
    <li><strong>Do not memorise word-for-word.</strong> Learn the structure, then speak naturally.</li>
    <li>For behavioural questions, follow <strong>STAR</strong>: Situation &rarr; Task &rarr; Action &rarr; Result.</li>
    <li><strong>Part 3 (Technical)</strong> is the section most candidates skip &mdash; don&apos;t.</li>
    <li>All answers are tailored to <strong>{_e(job.company)}</strong> and this specific role.</li>
  </ul>
</div>

<div class="controls">
  <button onclick="openAll()">&#x1F4D6; Show All Answers</button>
  <button class="secondary" onclick="closeAll()">&#x1F648; Hide All (Self-Test)</button>
  <button class="secondary" onclick="window.print()">&#x1F5A8; Print / Save as PDF</button>
</div>

{email_q_html}
<!-- PART 1 -->
<section class="part" id="part1">
  <h2>Part 1 &mdash; Company HR Questions</h2>
  <p class="part-intro">Motivation, background, company fit, and practical logistics. Expect every one of these.</p>
  {p1}
</section>

<!-- PART 2 -->
<section class="part" id="part2">
  <h2>Part 2 &mdash; SHL Behavioural Questions (STAR Format)</h2>
  <p class="part-intro">STAR = Situation &rarr; Task &rarr; Action &rarr; Result. Keep S+T to 20% of your answer. Focus time and detail on Action and Result.</p>
  {p2}
</section>

<!-- PART 3 -->
<section class="part" id="part3">
  <h2>Part 3 &mdash; Technical &amp; Domain Questions</h2>
  <p class="part-intro">These are role-specific. They test whether you actually know the tools and methods in the JD &mdash; not just whether you listed them on your CV. Prepare these carefully.</p>
  {p3}
</section>

<!-- PART 4 -->
<section class="part" id="part4">
  <h2>Part 4 &mdash; CV Defence (Defend Your Numbers)</h2>
  <p class="part-intro">Be ready to back up every metric on your CV. If they ask "how exactly was that measured?" &mdash; you must have a specific answer.</p>
  {p4}
</section>

<!-- PART 5 -->
<section class="part" id="part5">
  <h2>Part 5 &mdash; Questions to Ask Them</h2>
  <p class="part-intro">Always ask 3&ndash;5 thoughtful questions. Never say "I have no questions." It signals low interest.</p>
  <ol class="ask-list">
    {ask_li}
  </ol>
  <div class="warning"><strong>Avoid in the first interview:</strong> salary, vacation, remote work, or any requests before receiving an offer.</div>
</section>

<!-- PART 6 -->
<section class="part" id="part6">
  <h2>Part 6 &mdash; Tips &amp; German Context</h2>
  {tips}
</section>

<!-- PART 7 -->
<section class="part" id="part7">
  <h2>Part 7 &mdash; CV Bullet-Point STAR Defence</h2>
  <p class="part-intro">Every line on your CV is a potential interview question. Click any bullet to see the full STAR answer &mdash; know this section cold before you walk in.</p>
  {p7}
</section>

<footer>
  <div class="good-luck">{good_luck} &#x1F340;</div>
  <p>You have a strong profile. Trust your preparation and speak with confidence.</p>
</footer>

</div>

<script>
  function openAll()  {{ document.querySelectorAll('details.qa').forEach(d => d.open = true); }}
  function closeAll() {{
    document.querySelectorAll('details.qa').forEach(d => d.open = false);
    window.scrollTo({{ top: 0, behavior: 'smooth' }});
  }}
</script>
</body>
</html>"""


# -- Claude call helper --------------------------------------------------------

class InterviewPrepGenerator:
    def __init__(self, tracker=None):
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._tracker = tracker

    def _call(self, prompt: str, job_id: str, call_label: str) -> dict:
        system = _CANDIDATE_CONTEXT + "\n" + _BEHAVIOUR_RULES
        response = self.client.messages.create(
            model=_IP_MODEL,
            max_tokens=8000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        self._log_cost(job_id, call_label, response)
        stop_reason = response.stop_reason
        out_tokens = response.usage.output_tokens
        logger.info(
            "[interview_prep] %s -- stop_reason=%s  output_tokens=%d",
            call_label, stop_reason, out_tokens,
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "[interview_prep] %s JSON error at char %d. Last 300 chars:\n...%s",
                call_label, exc.pos, raw[max(0, exc.pos - 50): exc.pos + 100],
            )
            raise

    def _log_cost(self, job_id: str, label: str, response) -> None:
        if not self._tracker:
            return
        try:
            cost = calc_cost(
                _IP_MODEL,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            self._tracker.log_api_cost(
                job_id, label, _IP_MODEL,
                response.usage.input_tokens, response.usage.output_tokens, cost,
            )
        except Exception as exc:
            logger.debug(f"[interview_prep] cost log failed: {exc}")

    async def generate(
        self,
        job: JobListing,
        out_dir: Path,
        filename_suffix: str = "",
        email_body: str = "",
    ) -> Optional[Path]:
        import asyncio
        logger.info(f"[interview_prep] Starting 4-call generation for {job.title} @ {job.company}")

        desc = (job.description or "Not provided.")[:3000]
        fmt = dict(title=job.title, company=job.company, location=job.location, description=desc)

        try:
            # Call 1: HR Questions + Ask Them
            hr_data = await asyncio.to_thread(
                self._call,
                _PROMPT_HR.format(**fmt),
                job.job_id,
                "interview_prep_hr",
            )
            logger.info(f"[interview_prep] Call 1 (HR) done -- {len(hr_data)} top-level keys")

            # Call 2: STAR Behavioural only (16 QAs)
            star_data = await asyncio.to_thread(
                self._call,
                _PROMPT_STAR.format(**fmt),
                job.job_id,
                "interview_prep_star",
            )
            logger.info(f"[interview_prep] Call 2 (STAR) done -- {len(star_data)} top-level keys")

            # Call 3: Technical Domain + CV Defence (9 QAs)
            tech_data = await asyncio.to_thread(
                self._call,
                _PROMPT_TECH.format(**fmt),
                job.job_id,
                "interview_prep_tech",
            )
            logger.info(f"[interview_prep] Call 3 (Tech+CVD) done -- {len(tech_data)} top-level keys")

            # Call 4: CV Bullet-Point STAR Defence (18 bullets)
            cv_star_data = await asyncio.to_thread(
                self._call,
                _PROMPT_CV_STAR.format(title=job.title, company=job.company),
                job.job_id,
                "interview_prep_cv_star",
            )
            logger.info(f"[interview_prep] Call 4 (CV STAR) done -- {len(cv_star_data)} top-level keys")

        except json.JSONDecodeError as exc:
            logger.error(f"[interview_prep] JSON parse error: {exc}")
            return None
        except Exception as exc:
            logger.error(f"[interview_prep] Claude call failed: {exc}")
            return None

        # Call 5 (optional): Extract explicit questions from the interview email
        email_questions: list = []
        if email_body and email_body.strip():
            try:
                eq_prompt = _PROMPT_EMAIL_QUESTIONS.format(
                    profile=config.CV_PROFILE_TEXT[:2000],
                    title=job.title,
                    company=job.company,
                    email_body=email_body[:3000],
                )
                eq_data = await asyncio.to_thread(
                    self._call, eq_prompt, job.job_id, "interview_prep_email_q"
                )
                email_questions = eq_data.get("questions", [])
                if email_questions:
                    logger.info(
                        f"[interview_prep] Call 5 (Email Q) done -- "
                        f"{len(email_questions)} question(s) extracted"
                    )
                else:
                    logger.info("[interview_prep] Call 5 (Email Q) — no explicit questions found in email")
            except Exception as exc:
                logger.warning(f"[interview_prep] Email question extraction failed (non-fatal): {exc}")

        # Merge call 2 + call 3 into one dict for the renderer
        star_data.update(tech_data)

        html_content = _render(hr_data, star_data, cv_star_data, job, email_questions=email_questions or None)

        suffix = filename_suffix or job.company.replace(" ", "_")
        out_path = out_dir / f"Interview_Prep_{suffix}.html"
        out_path.write_text(html_content, encoding="utf-8")
        logger.info(f"[interview_prep] Saved -> {out_path.name} ({len(html_content)} chars)")
        return out_path
