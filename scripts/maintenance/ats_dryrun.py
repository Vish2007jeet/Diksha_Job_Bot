"""
Local ATS dry-run — ZERO API cost.

Approximates the Claude ATS auditor in ai/evaluator.py using the same deduction
table (HARD 8 / DOMAIN 5 / SOFT 2, start at 100) but scoring verbatim presence
in Python instead of asking a model. Use this to iterate on prompts and CV
content without spending API credit; only run the real pipeline once the local
number stops moving.

It is an approximation, not the auditor: it cannot judge synonym equivalence or
German/English mapping. Treat it as a directional signal and a missing-keyword
list, not a score to quote.

Usage:
    python scripts/maintenance/ats_dryrun.py <job_id> [path/to/CV.docx]

With no docx path it scores the CV in data/applications/Test_CV_CL for that job.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from docx import Document  # noqa: E402

from ai.cv_generator import classify_ats_keyword, _ATS_WEIGHT  # noqa: E402

_STOP = {
    "and", "the", "for", "with", "you", "your", "our", "are", "will", "who",
    "that", "this", "from", "have", "has", "not", "but", "all", "any", "can",
    "als", "und", "der", "die", "das", "mit", "für", "von", "ein", "eine",
}


def load_jd(job_id: str) -> tuple[str, str]:
    conn = sqlite3.connect(str(BASE / "data" / "jobs.db"))
    row = conn.execute(
        "SELECT title, company, description FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"job {job_id!r} not found")
    return f"{row[0]} @ {row[1]}", row[2] or ""


def cv_text(path: Path) -> str:
    return "\n".join(p.text for p in Document(str(path)).paragraphs)


def extract_terms(jd: str) -> list[str]:
    """Candidate JD keywords: capitalised phrases, known tool shapes, bigrams."""
    terms: set[str] = set()
    # Product/tool shapes: Power BI, SAP FI/CO, MS365, S/4HANA, Jira
    for m in re.finditer(r"\b[A-Z][A-Za-z0-9]*(?:[ /][A-Z0-9][A-Za-z0-9/]*)*\b", jd):
        t = m.group(0).strip()
        if len(t) > 2 and t.lower() not in _STOP and not t.isupper() or "/" in t:
            terms.add(t)
    # Domain bigrams: "project management", "data analysis"
    for m in re.finditer(r"\b([a-z]{4,})\s+(management|analysis|reporting|"
                         r"coordination|planning|automation|governance|support|"
                         r"optimisation|optimization|documentation)\b", jd, re.I):
        terms.add(m.group(0).strip())
    cleaned = {t for t in terms if 2 < len(t) < 40}
    return sorted(cleaned, key=str.lower)


def score(jd: str, cv: str) -> tuple[int, list[tuple[str, str, int]], list[str]]:
    cv_l = cv.lower()
    terms = extract_terms(jd)
    missing: list[tuple[str, str, int]] = []
    hit: list[str] = []
    for t in terms:
        if t.lower() in cv_l:
            hit.append(t)
        else:
            cls = classify_ats_keyword(t)
            missing.append((t, cls, _ATS_WEIGHT[cls]))
    penalty = sum(p for _, _, p in missing)
    return max(0, 100 - penalty), missing, hit


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    job_id = sys.argv[1]
    label, jd = load_jd(job_id)

    if len(sys.argv) > 2:
        cv_path = Path(sys.argv[2])
    else:
        root = BASE / "data" / "applications" / "Test_CV_CL"
        cands = sorted(root.glob("*/CV_*.docx"), key=lambda p: p.stat().st_mtime)
        if not cands:
            raise SystemExit(f"no CV docx found under {root}")
        cv_path = cands[-1]

    s, missing, hit = score(jd, cv_text(cv_path))
    print(f"JOB : {label}")
    print(f"CV  : {cv_path.name}")
    print(f"\nLOCAL ATS (approx): {s}/100   covered {len(hit)}  missing {len(missing)}")
    by_cls: dict[str, list[str]] = {}
    for t, c, _ in missing:
        by_cls.setdefault(c, []).append(t)
    for cls in ("HARD", "DOMAIN", "SOFT"):
        items = by_cls.get(cls, [])
        if items:
            print(f"\n  MISSING {cls} (-{_ATS_WEIGHT[cls]} each, {len(items)} terms):")
            for t in items[:25]:
                print(f"    - {t}")


if __name__ == "__main__":
    main()
