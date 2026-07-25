"""
Regenerate Interview Prep HTML for an existing application folder.

Usage (from D:\Job_Bot):
    python regen_interview_prep.py

The script re-generates the interview prep for the VW application using
the new Sudarshan-style template. You can also edit the job details below
to regenerate for any other application.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ai.interview_prep_generator import InterviewPrepGenerator
from utils.models import JobListing
import config


# ── Customize these per application ──────────────────────────────
APPLICATIONS = [
    {
        "folder": "1. Volkswagen_Group_Praktikum_Digitalisierte_Entwicklung_Bremsregelsys",
        "suffix": "Volkswagen_Group_Praktikum_Bremsregelsysteme",
        "job": JobListing(
            job_id="vw_brems_regen",
            source="linkedin",
            title="Praktikum Digitalisierte Entwicklung Bremsregelsysteme (w/m/d)",
            company="Volkswagen Group",
            location="Wolfsburg, Germany",
            url="https://www.volkswagen-group.com/careers",
            description=(
                "Mitarbeit bei der Entwicklung und Digitalisierung von Bremsregelsystemen (ABS, ESP, iBooster). "
                "Unterstützung bei der Modellbildung und Simulation in MATLAB/Simulink. "
                "Auswertung und Analyse von Messdaten aus Fahrversuchen. "
                "Anforderungen: Studium Fahrzeugtechnik/Maschinenbau; MATLAB/Simulink; Kenntnisse Bremsregelsysteme; "
                "Ansys oder ähnliche Simulationstools; erste Praxiserfahrung."
            ),
        ),
    },
]


async def main():
    gen = InterviewPrepGenerator()
    for item in APPLICATIONS:
        out_dir = config.OUTPUT_DIR / item["folder"]
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nGenerating: {item['job'].title} @ {item['job'].company}")
        path = await gen.generate(item["job"], out_dir=out_dir, filename_suffix=item["suffix"])
        if path:
            print(f"  Saved -> {path}")
        else:
            print(f"  FAILED — check logs")


if __name__ == "__main__":
    asyncio.run(main())
