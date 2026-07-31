from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "legalpdf.document.v2"
PARSER_VERSION = "0.3.0"


@dataclass(slots=True)
class Word:
    id: str
    text: str
    bbox: list[float]
    start: int
    end: int


@dataclass(slots=True)
class Span:
    id: str
    text: str
    bbox: list[float]
    font: str = ""
    size: float = 0.0
    flags: int = 0
    superscript: bool = False
    start: int = 0
    end: int = 0


@dataclass(slots=True)
class Line:
    id: str
    page_index: int
    page_number: int
    source_index: int
    reading_order: int
    block_index: int
    text: str
    bbox: list[float]
    spans: list[Span] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    detached_references: list[dict[str, Any]] = field(default_factory=list)
    exclude_from_body: bool = False
    suppress_footnote_label: bool = False
    note_region_mode: str = ""
    region_id: str = ""
    region_type: str = "unknown"
    source: str = "native"


@dataclass(slots=True)
class Region:
    id: str
    page_index: int
    type: str
    line_ids: list[str]
    bbox: list[float]
    reading_order: int


@dataclass(slots=True)
class Page:
    id: str
    index: int
    number: int
    width: float
    height: float
    lines: list[Line]
    regions: list[Region]
    source: str = "native"
    text_quality: float = 1.0
    printed_label: str | None = None
    printed_label_source: str | None = None
    printed_label_line_id: str | None = None


@dataclass(slots=True)
class Paragraph:
    id: str
    page_index: int
    region_type: str
    text: str
    line_ids: list[str]
    anchors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Section:
    id: str
    heading_paragraph_id: str
    heading: str
    locator: str
    locator_kind: str | None
    aliases: list[str]
    text: str
    paragraph_ids: list[str]
    page_indexes: list[int]
    line_ids: list[str]
    provenance: str = "heading-region"


@dataclass(slots=True)
class Footnote:
    pair_id: str
    label: str
    occurrence: int
    restart_sequence: int
    reference_page: int | None
    body_pages: list[int]
    reference_line_id: str | None
    body_line_ids: list[str]
    body: str
    sentence_proposition: str
    passage_since_prior_note: str
    confidence: float
    provenance: str
    warnings: list[str] = field(default_factory=list)
    crossrefs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    page_index: int | None = None
    line_ids: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RepairRecord:
    page_index: int
    status: str
    model: str
    effort: str
    prompt_version: str
    cache_key: str
    attempts: int
    elapsed_seconds: float
    input_line_hash: str
    output_hash: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)
    error: str = ""
    scope_pages: list[int] = field(default_factory=list)


@dataclass(slots=True)
class LegalDocument:
    document_id: str
    source_name: str
    source_sha256: str
    page_count: int
    status: str
    pages: list[Page]
    paragraphs: list[Paragraph]
    sections: list[Section]
    footnotes: list[Footnote]
    diagnostics: list[Diagnostic]
    repairs: list[RepairRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    parser_version: str = PARSER_VERSION

    @property
    def lines(self) -> list[Line]:
        return [line for page in self.pages for line in page.lines]

    @property
    def text(self) -> str:
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "document_id": self.document_id,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "page_count": self.page_count,
            "status": self.status,
            "metadata": self.metadata,
            "provenance": self.provenance,
            "counts": {
                "pages": len(self.pages),
                "lines": len(self.lines),
                "paragraphs": len(self.paragraphs),
                "sections": len(self.sections),
                "footnotes": len(self.footnotes),
                "diagnostics": len(self.diagnostics),
                "repairs": len(self.repairs),
            },
            "artifacts": {
                "pages": "pages.jsonl",
                "paragraphs": "paragraphs.jsonl",
                "sections": "sections.jsonl",
                "footnotes": "footnotes.jsonl",
                "diagnostics": "diagnostics.jsonl",
                "repairs": "repairs.jsonl",
            },
        }


@dataclass(slots=True)
class FootnoteLookup:
    status: str
    query: str
    matches: list[str]
    footnote: Footnote | None = None
    proposition_mode: str = "sentence"
    proposition: str = ""
    context: str = ""


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_jsonl(path: Path, values: Iterable[Any]) -> None:
    _atomic_write(path, "".join(_json_line(asdict(value)) for value in values))


def write_artifacts(document: LegalDocument, output_dir: str | Path) -> Path:
    """Write collections first and publish ``document.json`` last."""

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "document.json"
    manifest_path.unlink(missing_ok=True)
    _write_jsonl(root / "pages.jsonl", document.pages)
    _write_jsonl(root / "paragraphs.jsonl", document.paragraphs)
    _write_jsonl(root / "sections.jsonl", document.sections)
    _write_jsonl(root / "footnotes.jsonl", document.footnotes)
    _write_jsonl(root / "diagnostics.jsonl", document.diagnostics)
    _write_jsonl(root / "repairs.jsonl", document.repairs)
    _atomic_write(
        manifest_path,
        json.dumps(document.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest_path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        rows = []
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(
                    f"Artifact {path.name} line {line_number} is not an object"
                )
            rows.append(value)
        return rows


def _artifact_paths(root: Path, manifest: dict[str, Any]) -> dict[str, Path]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("Document manifest has no artifact map")
    paths: dict[str, Path] = {}
    for key in (
        "pages",
        "paragraphs",
        "sections",
        "footnotes",
        "diagnostics",
        "repairs",
    ):
        name = artifacts.get(key)
        if not isinstance(name, str) or not name:
            raise ValueError(f"Document manifest has no {key} artifact")
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root) or candidate == root:
            raise ValueError(f"Document manifest has an unsafe {key} artifact path")
        paths[key] = candidate
    return paths


def _load_artifact_manifest(
    path: str | Path,
) -> tuple[dict[str, Any], dict[str, Path]]:
    manifest_path = Path(path).expanduser().resolve()
    if manifest_path.is_dir():
        manifest_path /= "document.json"
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("Document manifest is not an object")
    required = {
        "schema_version",
        "parser_version",
        "document_id",
        "source_name",
        "source_sha256",
        "page_count",
        "status",
        "metadata",
        "provenance",
        "counts",
        "artifacts",
    }
    if missing := required - manifest.keys():
        raise ValueError(
            f"Document manifest is missing fields: {', '.join(sorted(missing))}"
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported document schema: {manifest.get('schema_version')!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )
    if manifest.get("parser_version") != PARSER_VERSION:
        raise ValueError(
            f"Unsupported parser version: {manifest.get('parser_version')!r}; "
            f"expected {PARSER_VERSION!r}"
        )
    return manifest, _artifact_paths(manifest_path.parent, manifest)


def _require_fields(value: dict[str, Any], kind: type[Any]) -> None:
    if missing := {item.name for item in fields(kind)} - value.keys():
        raise ValueError(
            f"{kind.__name__} artifact is missing fields: "
            f"{', '.join(sorted(missing))}"
        )


def _page_from_dict(value: dict[str, Any]) -> Page:
    _require_fields(value, Page)
    for line in value["lines"]:
        _require_fields(line, Line)
        for span in line["spans"]:
            _require_fields(span, Span)
        for word in line["words"]:
            _require_fields(word, Word)
    for region in value["regions"]:
        _require_fields(region, Region)
    return Page(
        id=value["id"],
        index=int(value["index"]),
        number=int(value["number"]),
        width=float(value["width"]),
        height=float(value["height"]),
        lines=[
            Line(
                **{
                    **line,
                    "spans": [Span(**span) for span in line["spans"]],
                    "words": [Word(**word) for word in line["words"]],
                }
            )
            for line in value["lines"]
        ],
        regions=[Region(**region) for region in value["regions"]],
        source=value["source"],
        text_quality=float(value["text_quality"]),
        printed_label=value["printed_label"],
        printed_label_source=value["printed_label_source"],
        printed_label_line_id=value["printed_label_line_id"],
    )


def load_artifacts(path: str | Path) -> LegalDocument:
    manifest, artifacts = _load_artifact_manifest(path)
    pages = [_page_from_dict(value) for value in _read_jsonl(artifacts["pages"])]
    rows = _read_jsonl(artifacts["paragraphs"])
    for value in rows:
        _require_fields(value, Paragraph)
    paragraphs = [Paragraph(**value) for value in rows]
    rows = _read_jsonl(artifacts["sections"])
    for value in rows:
        _require_fields(value, Section)
    sections = [Section(**value) for value in rows]
    rows = _read_jsonl(artifacts["footnotes"])
    for value in rows:
        _require_fields(value, Footnote)
    footnotes = [Footnote(**value) for value in rows]
    rows = _read_jsonl(artifacts["diagnostics"])
    for value in rows:
        _require_fields(value, Diagnostic)
    diagnostics = [Diagnostic(**value) for value in rows]
    rows = _read_jsonl(artifacts["repairs"])
    for value in rows:
        _require_fields(value, RepairRecord)
    repairs = [RepairRecord(**value) for value in rows]
    expected_counts = manifest["counts"]
    actual_counts = {
        "pages": len(pages),
        "lines": sum(len(page.lines) for page in pages),
        "paragraphs": len(paragraphs),
        "sections": len(sections),
        "footnotes": len(footnotes),
        "diagnostics": len(diagnostics),
        "repairs": len(repairs),
    }
    if not isinstance(expected_counts, dict) or any(
        expected_counts.get(key) != value for key, value in actual_counts.items()
    ):
        raise ValueError("Document artifact counts do not match the manifest")
    if manifest["page_count"] != len(pages):
        raise ValueError("Document page_count does not match the page artifact")
    return LegalDocument(
        document_id=manifest["document_id"],
        source_name=manifest["source_name"],
        source_sha256=manifest["source_sha256"],
        page_count=int(manifest["page_count"]),
        status=manifest["status"],
        pages=pages,
        paragraphs=paragraphs,
        sections=sections,
        footnotes=footnotes,
        diagnostics=diagnostics,
        repairs=repairs,
        metadata=manifest["metadata"],
        provenance=manifest["provenance"],
        schema_version=manifest["schema_version"],
        parser_version=manifest["parser_version"],
    )
