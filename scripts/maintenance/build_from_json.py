"""
Build CV + CL documents from a JSON file — ZERO API cost.

The normal pipeline calls Claude to write the content. This script skips that
entirely: you supply the content as JSON (written by hand, or by Claude Code in
an interactive session under your subscription), and it runs everything the
pipeline does afterwards — every free validator, the template fill, and the PDF
export.

Use it when you want the bot's document quality without spending API credit.

Usage:
    python scripts/maintenance/build_from_json.py content.json [out_dir_name]

Expected JSON shape:
{
  "company":  "eigenblue",
  "role":     "Werkstudent_Project_Management",
  "cv": {
    "summary": "...",
    "chintamani": ["...", "...", "...", "..."],
    "accenture":  ["...", "...", "...", "..."],
    "project1_desc": "...",
    "project1_bullets": ["...", "...", "..."],
    "project2_desc": "...",
    "project2_bullets": ["...", "...", "..."]
  },
  "cl": {
    "company_name": "...", "company_addr": "...", "subject_line": "...",
    "para1": "...", "para2": "...", "para3": "...", "para4": "...", "para5": "..."
  }
}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

import config  # noqa: E402
from ai.evaluator import (check_banned_words, cv_dict_to_text,  # noqa: E402
                          cl_dict_to_text)
from documents.exporter import DocumentExporter  # noqa: E402
from documents.pipeline import (_check_cv_word_counts,  # noqa: E402
                                _check_forbidden_verbs,
                                _check_cl_closing_bans,
                                _check_paragraph_endings,
                                _check_para1_opening,
                                CV_FILENAME, CL_FILENAME)
from documents.template_engine import TemplateEngine  # noqa: E402


def _report(title: str, issues: list[str]) -> int:
    if issues:
        print(f"  [!] {title}: {len(issues)}")
        for i in issues:
            print(f"        - {i}")
        return len(issues)
    print(f"  [ok] {title}")
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    cv, cl = data.get("cv") or {}, data.get("cl") or {}
    company = data.get("company", "Company")
    role = data.get("role", "Role")

    folder_name = sys.argv[2] if len(sys.argv) > 2 else f"{company}_{role}"
    out_dir = BASE / "data" / "applications" / "Manual_Build" / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    print("VALIDATORS (the same gates the paid pipeline applies)\n")
    problems = 0
    problems += _report("CV word counts", _check_cv_word_counts(cv))
    problems += _report("CV seniority verbs", _check_forbidden_verbs(cv))
    problems += _report("CV banned words", check_banned_words(cv_dict_to_text(cv)))
    if cl:
        problems += _report("CL banned words", check_banned_words(cl_dict_to_text(cl)))
        problems += _report("CL closing bans", _check_cl_closing_bans(cl))
        problems += _report("CL paragraph endings", _check_paragraph_endings(cl))
        opener = _check_para1_opening(cl)
        problems += _report("CL opening line", [opener] if opener else [])

    counts = (f"  summary {len(cv.get('summary','').split())}w | "
              f"chintamani {len(cv.get('chintamani', []))} | "
              f"accenture {len(cv.get('accenture', []))} | "
              f"proj1 {len(cv.get('project1_bullets', []))} | "
              f"proj2 {len(cv.get('project2_bullets', []))}")
    print(f"\nSECTION COUNTS\n{counts}")

    print("\nBUILD")
    engine, exporter = TemplateEngine(), DocumentExporter()
    cv_docx = out_dir / f"{CV_FILENAME}_{folder_name}.docx"
    engine.apply_cv_content(config.CV_TEMPLATE_PATH, cv, cv_docx)
    cv_pdf = exporter.to_pdf(cv_docx)
    print(f"  CV  {cv_docx.name}")
    print(f"      {Path(cv_pdf).name if cv_pdf else 'PDF export failed'}")

    if cl:
        cl_docx = out_dir / f"{CL_FILENAME}_{folder_name}.docx"
        engine.apply_cl_content(config.CL_TEMPLATE_PATH, cl, cl_docx)
        cl_pdf = exporter.to_pdf(cl_docx)
        print(f"  CL  {cl_docx.name}")
        print(f"      {Path(cl_pdf).name if cl_pdf else 'PDF export failed'}")

    print(f"\n  folder: {out_dir}")
    print(f"  validator issues: {problems}   API cost: $0.00")


if __name__ == "__main__":
    main()
