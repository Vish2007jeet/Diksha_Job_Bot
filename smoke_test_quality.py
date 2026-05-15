"""
Quality Score Smoke Test
========================
Generates one CV + one Cover Letter for a realistic sample job and
prints a side-by-side comparison of three independent scorers:

  Column 1 — Self        : Claude's own self-assessment (known to inflate)
  Column 2 — Evaluator   : Independent Claude auditor (separate prompt)
  Column 3 — Sapling     : External AI-detection API (free tier)
                           Set SAPLING_API_KEY in .env to activate.
                           Shows "n/a" if key is not set.

Run:
    python smoke_test_quality.py
"""
import asyncio
import os
import sys
import time

# ── Bootstrap — must happen before ANY config/ai imports ──────────
JOB_BOT = r"D:\Job_Bot"
sys.path.insert(0, JOB_BOT)
os.chdir(JOB_BOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(os.path.join(JOB_BOT, ".env"), override=True)

# ── All env-dependent imports AFTER dotenv load ───────────────────
import config                                          # reads ANTHROPIC_API_KEY
from ai.cv_generator import CVGenerator
from ai.evaluator import DocumentEvaluator
from ai.humanizer import ContentHumanizer
from utils.models import JobListing

# ── Colour helpers ─────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def _col(score, threshold=95):
    if score == 0:   return DIM
    if score >= threshold: return GREEN
    if score >= threshold - 10: return YELLOW
    return RED

def _fmt(score, threshold=95):
    if score == 0:
        return f"{DIM}  n/a {RESET}"
    c = _col(score, threshold)
    return f"{c}{score:>4}%{RESET}"

# ── Sample job — realistic Werkstudent Simulation JD ─────────────
SAMPLE_JD = """
Werkstudent (m/w/d) Vehicle Dynamics Simulation & Virtual Validation

BMW Group | Munich, Germany | Part-time, 20h/week

Your responsibilities:
- Support the vehicle dynamics simulation team in developing and validating
  virtual vehicle models using Adams Car and CarMaker.
- Conduct lap time simulations and handling analyses in MATLAB/Simulink for
  suspension kinematics and compliance studies.
- Process and analyse measurement data from test drives using Python and
  Power BI dashboards to benchmark virtual vs. physical results.
- Assist in parameter identification and model correlation for full-vehicle
  MBS (Multi-Body Simulation) models in Adams MBD.
- Contribute to the development of automated regression test pipelines in
  Python for simulation model validation (CI/CD).
- Support tire model parameterisation (MF-Tyre/Swift) and interface with
  the chassis team on suspension design targets.
- Document simulation methods and results using Confluence and JIRA.

Your profile:
- Student (m/w/d) in Automotive Engineering, Mechanical Engineering,
  Mechatronics or similar field.
- Strong knowledge of vehicle dynamics fundamentals (kinematics, compliance,
  handling, ride).
- Practical experience with Adams Car, Adams MBD, or CarMaker.
- Proficiency in MATLAB/Simulink for modelling and data analysis.
- Python scripting experience for automation and data processing.
- Familiarity with Power BI or similar BI tools for result visualisation.
- Knowledge of MF-Tyre or Swift tire models is a plus.
- Fluent in English; German is advantageous.
- Immediately available for at least 6 months.
""".strip()

# ── Pretty table printer ───────────────────────────────────────────
def _table(doc: str, before: int, after: int) -> None:
    W = 58
    print(f"\n{BOLD}{CYAN}{'─'*W}")
    print(f"  {doc}")
    print(f"{'─'*W}{RESET}")
    hdr = f"  {'Metric':<20}  {'Before':>8}  {'After':>8}  {'Delta':>7}"
    print(f"{BOLD}{hdr}{RESET}")
    print(f"  {'─'*20}  {'─'*8}  {'─'*8}  {'─'*7}")

    delta = after - before
    delta_col = GREEN if delta >= 0 else RED
    print(
        f"  {'ATS Score':<20}  {_fmt(before, 95)}  {_fmt(after, 95)}  "
        f"{delta_col}{delta:+d}{RESET}"
    )
    print(f"  {'─'*20}  {'─'*8}  {'─'*8}  {'─'*7}")


# ── Main ───────────────────────────────────────────────────────────
async def main():
    print(f"\n{BOLD}{CYAN}{'='*58}")
    print("  Quality Smoke Test — Before vs After Humanizer")
    print(f"{'='*58}{RESET}")
    print(f"  Job  : Werkstudent Vehicle Dynamics Simulation @ BMW Group")
    print(f"  Model: {config.CLAUDE_MODEL}  |  Humanizer: Haiku 4.5")
    print(f"{'─'*58}\n")

    job = JobListing(
        job_id="smoke_quality_test_001",
        source="smoke_test",
        title="Werkstudent (m/w/d) Vehicle Dynamics Simulation & Virtual Validation",
        company="BMW Group",
        location="Munich, Germany",
        url="https://bmw.com/careers/smoke-test",
        description=SAMPLE_JD,
    )

    gen       = CVGenerator(tracker=None)
    humanizer = ContentHumanizer(tracker=None)
    evaluator = DocumentEvaluator(tracker=None)

    # ── Generate ───────────────────────────────────────────────────
    print(f"{BOLD}Step 1 — Generating CV + CL ...{RESET}")
    t0 = time.time()
    cv_data, cl_data = await asyncio.gather(
        gen.generate_cv_content(job),
        gen.generate_cl_content(job),
    )
    print(f"  Done in {time.time()-t0:.1f}s\n")

    # ── Evaluate (before humanizer) ────────────────────────────────
    print(f"{BOLD}Step 2 — Evaluating (before humanizer) ...{RESET}")
    t1 = time.time()
    cv_before, cl_before = await asyncio.gather(
        evaluator.evaluate_cv(job.job_id, SAMPLE_JD, cv_data),
        evaluator.evaluate_cl(job.job_id, SAMPLE_JD, cl_data),
    )
    print(f"  Done in {time.time()-t1:.1f}s\n")

    # ── Humanizer rewrite ──────────────────────────────────────────
    print(f"{BOLD}Step 3 — Humanizer rewrite (Haiku) ...{RESET}")
    t2 = time.time()
    cv_data, cl_data = await asyncio.gather(
        humanizer.humanize_cv(job.job_id, cv_data),
        humanizer.humanize_cl(job.job_id, cl_data),
    )
    print(f"  Done in {time.time()-t2:.1f}s\n")

    # ── Evaluate (after humanizer) ─────────────────────────────────
    print(f"{BOLD}Step 4 — Evaluating (after humanizer) ...{RESET}")
    t3 = time.time()
    cv_after, cl_after = await asyncio.gather(
        evaluator.evaluate_cv(job.job_id, SAMPLE_JD, cv_data),
        evaluator.evaluate_cl(job.job_id, SAMPLE_JD, cl_data),
    )
    print(f"  Done in {time.time()-t3:.1f}s\n")

    # ── Results ────────────────────────────────────────────────────
    _table("CV", cv_before.ats_score, cv_after.ats_score)
    if cv_after.banned_words_found:
        print(f"\n  {RED}🚫 Banned words in CV: {', '.join(cv_after.banned_words_found)}{RESET}")
    if cv_after.missing_keywords:
        print(f"\n  {YELLOW}⚠️  ATS Gaps (CV, top 5):{RESET}")
        for g in cv_after.missing_keywords[:5]:
            print(f"     • {g}")

    _table("Cover Letter", cl_before.ats_score, cl_after.ats_score)
    if cl_after.banned_words_found:
        print(f"\n  {RED}🚫 Banned words in CL: {', '.join(cl_after.banned_words_found)}{RESET}")

    # ── Summary ────────────────────────────────────────────────────
    total_time = time.time() - t0
    print(f"\n{BOLD}{CYAN}{'─'*58}")
    print(f"  Summary")
    print(f"{'─'*58}{RESET}")
    print(f"  CV  : ATS {'✅' if cv_after.ats_score >= 95 else '❌'} {cv_after.ats_score}  (was {cv_before.ats_score})")
    print(f"  CL  : ATS {'✅' if cl_after.ats_score >= 95 else '❌'} {cl_after.ats_score}  (was {cl_before.ats_score})")
    print(f"  Time: {total_time:.1f}s total")

    overall_pass = (
        cv_after.ats_score >= 95 and not cv_after.banned_words_found and
        cl_after.ats_score >= 95 and not cl_after.banned_words_found
    )
    if overall_pass:
        print(f"\n  {GREEN}{BOLD}✅ PASS — Both documents meet the quality threshold.{RESET}")
    else:
        print(f"\n  {RED}{BOLD}❌ FAIL — One or more scores below threshold.{RESET}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
