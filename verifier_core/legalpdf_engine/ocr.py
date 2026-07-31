from __future__ import annotations

import csv
import io
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol


@dataclass(slots=True)
class OCRLine:
    text: str
    bbox: list[float]
    confidence: float = 0.0


class OCRProvider(Protocol):
    """Optional page OCR boundary; implementations must return PDF coordinates."""

    name: str

    def extract_page(
        self,
        pdf_path: Path,
        page_index: int,
        *,
        width: float,
        height: float,
    ) -> list[OCRLine]: ...


class TesseractOCRProvider:
    """Render and OCR one requested PDF page with the Tesseract executable."""

    def __init__(
        self,
        *,
        command: str | Path | None = None,
        language: str = "eng",
        dpi: int = 180,
        psm: int = 3,
        timeout_seconds: int = 120,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_+-]+", language):
            raise ValueError("OCR language must be a Tesseract language code.")
        if not 72 <= dpi <= 600:
            raise ValueError("OCR DPI must be between 72 and 600.")
        if not 0 <= psm <= 13:
            raise ValueError("Tesseract page segmentation mode must be 0 through 13.")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("OCR timeout must be between 1 and 3600 seconds.")
        self.command = str(
            command or os.environ.get("LEGALPDF_TESSERACT_COMMAND") or "tesseract"
        )
        self.language = language
        self.dpi = dpi
        self.psm = psm
        self.timeout_seconds = timeout_seconds
        version = self._run(["--version"], timeout_seconds=10)
        version_line = (version.stdout or version.stderr).splitlines()
        identity = (
            re.sub(r"[\x00-\x1f\x7f]+", " ", version_line[0]).strip()[:200]
            if version_line
            else "unknown"
        )
        self.identity = f"tesseract-cli-v1:{identity or 'unknown'}"
        self.name = (
            f"{self.identity}:lang={language}:dpi={dpi}:psm={psm}"
        )

    def _run(
        self, arguments: list[str], *, timeout_seconds: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.command, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds or self.timeout_seconds,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "Tesseract was not found. Install Tesseract or set "
                "LEGALPDF_TESSERACT_COMMAND."
            ) from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Tesseract OCR timed out.") from error
        if result.returncode:
            raise RuntimeError(
                f"Tesseract OCR failed with exit code {result.returncode}."
            )
        return result

    def extract_page(
        self,
        pdf_path: Path,
        page_index: int,
        *,
        width: float,
        height: float,
    ) -> list[OCRLine]:
        try:
            import fitz
        except ImportError as error:
            raise RuntimeError("Tesseract OCR requires PyMuPDF.") from error
        with fitz.open(pdf_path) as document:
            if page_index < 0 or page_index >= document.page_count:
                raise IndexError(f"PDF page index is out of range: {page_index}")
            pixmap = document.load_page(page_index).get_pixmap(
                dpi=self.dpi, alpha=False
            )
            with TemporaryDirectory(prefix="legalpdf-tesseract-") as temporary:
                image = Path(temporary) / "page.png"
                pixmap.save(image)
                result = self._run(
                    [
                        str(image),
                        "stdout",
                        "-l",
                        self.language,
                        "--dpi",
                        str(self.dpi),
                        "--psm",
                        str(self.psm),
                        "tsv",
                    ]
                )
        return _tsv_lines(
            result.stdout,
            x_scale=width / pixmap.width,
            y_scale=height / pixmap.height,
            page_width=width,
            page_height=height,
        )


def _tsv_lines(
    value: str,
    *,
    x_scale: float,
    y_scale: float,
    page_width: float,
    page_height: float,
) -> list[OCRLine]:
    groups: dict[tuple[str, str, str, str], list[tuple[int, str, list[float], float]]] = {}
    for row in csv.DictReader(io.StringIO(value), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text:
            continue
        try:
            left = float(row["left"])
            top = float(row["top"])
            word_width = float(row["width"])
            word_height = float(row["height"])
            confidence = float(row.get("conf") or 0)
            word_number = int(row.get("word_num") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if word_width <= 0 or word_height <= 0:
            continue
        key = tuple(
            row.get(name) or "0"
            for name in ("page_num", "block_num", "par_num", "line_num")
        )
        groups.setdefault(key, []).append(
            (
                word_number,
                text,
                [
                    max(0.0, left * x_scale),
                    max(0.0, top * y_scale),
                    min(page_width, (left + word_width) * x_scale),
                    min(page_height, (top + word_height) * y_scale),
                ],
                confidence,
            )
        )
    lines: list[OCRLine] = []
    for words in groups.values():
        words.sort(key=lambda item: item[0])
        boxes = [word[2] for word in words]
        confidences = [word[3] for word in words if word[3] >= 0]
        lines.append(
            OCRLine(
                " ".join(word[1] for word in words),
                [
                    min(box[0] for box in boxes),
                    min(box[1] for box in boxes),
                    max(box[2] for box in boxes),
                    max(box[3] for box in boxes),
                ],
                (
                    max(0.0, min(1.0, sum(confidences) / len(confidences) / 100))
                    if confidences
                    else 0.0
                ),
            )
        )
    return lines
