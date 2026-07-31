"""Verify or refresh ALR's self-contained legal-PDF runtime snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = ROOT / "verifier_core" / "legalpdf_engine"
MANIFEST_PATH = VENDOR_ROOT / "VENDORED.json"
RUNTIME_FILES = (
    "adapters.py",
    "anchored_scan.py",
    "column_order_arbiter.py",
    "core.py",
    "data/mcgill_reporters.json",
    "footnote_pairing.py",
    "footnote_pairing_support.py",
    "footnote_separator_scan.py",
    "grammar_tables.py",
    "model.py",
    "note_crossrefs.py",
    "ocr.py",
    "superscript_splice.py",
)
GRAMMAR_FILES = (
    "citations.json",
    "footnote-labels.json",
    "pinpoints.json",
    "references.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(engine: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(engine), *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()


def _engine_state(engine: Path) -> tuple[str, Path, Path]:
    engine = engine.expanduser().resolve()
    runtime = engine / "src" / "legalpdf"
    grammar = engine / "data" / "grammar-tables"
    if not runtime.is_dir() or not grammar.is_dir():
        raise ValueError(f"Not a universal-legal-pdf-engine checkout: {engine}")
    dirty = _git(engine, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise ValueError("Engine checkout has uncommitted tracked changes")
    return _git(engine, "rev-parse", "HEAD"), runtime, grammar


def _manifest() -> dict:
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("VENDORED.json must contain an object")
    return value


def check(engine: Path | None = None) -> None:
    manifest = _manifest()
    if manifest.get("source") != "universal-legal-pdf-engine":
        raise ValueError("Unexpected vendored engine source")
    expected_files = manifest.get("files")
    expected_grammar = manifest.get("grammar_tables")
    if set(expected_files or {}) != set(RUNTIME_FILES):
        raise ValueError("Vendored runtime file list is incomplete or unexpected")
    if set(expected_grammar or {}) != set(GRAMMAR_FILES):
        raise ValueError("Vendored grammar file list is incomplete or unexpected")
    for relative, expected in expected_files.items():
        if _sha256(VENDOR_ROOT / relative) != expected:
            raise ValueError(f"Vendored file hash mismatch: {relative}")
    grammar_root = ROOT / "data" / "grammar-tables"
    for relative, expected in expected_grammar.items():
        if _sha256(grammar_root / relative) != expected:
            raise ValueError(f"Vendored grammar hash mismatch: {relative}")
    if engine is None:
        return
    commit, runtime, grammar = _engine_state(engine)
    if manifest.get("commit") != commit:
        raise ValueError(f"Vendored commit is {manifest.get('commit')}; engine is {commit}")
    for relative, expected in expected_files.items():
        if _sha256(runtime / relative) != expected:
            raise ValueError(f"Engine differs from vendored file: {relative}")
    for relative, expected in expected_grammar.items():
        if _sha256(grammar / relative) != expected:
            raise ValueError(f"Engine differs from vendored grammar: {relative}")


def sync(engine: Path) -> None:
    commit, runtime, grammar = _engine_state(engine)
    for relative in RUNTIME_FILES:
        destination = VENDOR_ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(runtime / relative, destination)
    grammar_root = ROOT / "data" / "grammar-tables"
    grammar_root.mkdir(parents=True, exist_ok=True)
    for relative in GRAMMAR_FILES:
        shutil.copyfile(grammar / relative, grammar_root / relative)
    manifest = {
        "source": "universal-legal-pdf-engine",
        "commit": commit,
        "files": {
            relative: _sha256(VENDOR_ROOT / relative)
            for relative in RUNTIME_FILES
        },
        "grammar_tables": {
            relative: _sha256(grammar_root / relative)
            for relative in GRAMMAR_FILES
        },
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    check(engine)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--engine", type=Path)
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--engine", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "sync":
        sync(args.engine)
    else:
        check(args.engine)
    print("Legal-PDF vendor snapshot verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
