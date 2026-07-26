"""Source-neutral document input consumed by the verifier pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ParsedDocument:
    """The small common model shared by DOCX and experimental PDF intake."""

    paragraphs: list[dict[str, Any]]
    footnotes: dict[int, str]
    author_links: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    source_path: Path | None = None
    source_kind: str = "DOCX"
    metadata: dict[str, Any] = field(default_factory=dict)
    footnote_order: list[int] = field(default_factory=list)
