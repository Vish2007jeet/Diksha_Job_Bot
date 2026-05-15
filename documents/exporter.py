"""
Document Exporter — converts DOCX → PDF.

Primary method:  docx2pdf  (uses Microsoft Word COM on Windows — best quality)
Fallback method: LibreOffice CLI (cross-platform)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from utils.logger import logger


class DocumentExporter:
    """Convert DOCX files to PDF."""

    def to_pdf(self, docx_path: Path) -> Path:
        """Convert a DOCX to PDF. Returns path to the PDF file."""
        pdf_path = docx_path.with_suffix(".pdf")

        if self._try_docx2pdf(docx_path, pdf_path):
            return pdf_path

        if self._try_libreoffice(docx_path, pdf_path):
            return pdf_path

        # Last resort: reportlab placeholder PDF
        self._fallback_pdf(docx_path, pdf_path)
        return pdf_path

    # ── Conversion Methods ─────────────────────────────────────

    def _try_docx2pdf(self, docx_path: Path, pdf_path: Path) -> bool:
        """Use docx2pdf (requires Microsoft Word on Windows / macOS)."""
        try:
            from docx2pdf import convert
            convert(str(docx_path), str(pdf_path))
            logger.info(f"PDF created via docx2pdf: {pdf_path.name}")
            return True
        except ImportError:
            logger.debug("docx2pdf not available")
        except Exception as exc:
            logger.warning(f"docx2pdf failed: {exc}")
        return False

    def _try_libreoffice(self, docx_path: Path, pdf_path: Path) -> bool:
        """Use LibreOffice headless (cross-platform fallback)."""
        lo_cmd = self._find_libreoffice()
        if not lo_cmd:
            logger.debug("LibreOffice not found")
            return False

        try:
            result = subprocess.run(
                [
                    lo_cmd,
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", str(pdf_path.parent),
                    str(docx_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            # LibreOffice writes <filename>.pdf in outdir
            lo_output = docx_path.parent / (docx_path.stem + ".pdf")
            if lo_output.exists():
                if lo_output != pdf_path:
                    lo_output.rename(pdf_path)
                logger.info(f"PDF created via LibreOffice: {pdf_path.name}")
                return True
        except Exception as exc:
            logger.warning(f"LibreOffice conversion failed: {exc}")
        return False

    def _fallback_pdf(self, docx_path: Path, pdf_path: Path) -> None:
        """
        Generate a very basic placeholder PDF using reportlab
        containing a note that the DOCX could not be auto-converted.
        """
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas

            c = canvas.Canvas(str(pdf_path), pagesize=A4)
            c.setFont("Helvetica", 14)
            c.drawString(72, 750, f"Document: {docx_path.name}")
            c.setFont("Helvetica", 11)
            c.drawString(72, 720, "PDF conversion requires Microsoft Word or LibreOffice.")
            c.drawString(72, 700, "Please open the .docx file and export manually.")
            c.save()
            logger.warning(f"Fallback placeholder PDF created: {pdf_path.name}")
        except Exception as exc:
            logger.error(f"Even fallback PDF creation failed: {exc}")

    @staticmethod
    def _find_libreoffice() -> str | None:
        """Find LibreOffice executable across platforms."""
        candidates = [
            "libreoffice",
            "soffice",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            "/usr/bin/libreoffice",
            "/usr/bin/soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
        for cmd in candidates:
            if shutil.which(cmd):
                return cmd
            if Path(cmd).exists():
                return cmd
        return None
