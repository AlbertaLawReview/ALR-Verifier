"""One-time extraction of large data files appended to the frozen exe.

The onefile PyInstaller bootloader extracts every archived byte to a fresh
%TEMP% dir on *every* launch, which made startup scale with the bundled
SQLite databases (1.2-1.6 GB). build_exe.py therefore keeps the databases
out of the PyInstaller archive and appends them to the finished exe as an
overlay:

    [pyinstaller exe][entry bytes...][footer JSON][u64 footer len]
    [b"ALRVOVL1"][88-byte corrected PyInstaller cookie]

The trailing cookie is a copy of the bootloader's archive cookie with its
archive-length field grown by the overlay size, so the bootloader's
backwards magic search finds it in the first read and still computes the
original archive offset (this also keeps launch from paying a backwards
scan over the whole overlay).

At runtime, db_path(name) copies the entry once into
%LOCALAPPDATA%/ALR Quote Verifier/data/<stem>-<sha12><ext> (content-hash
keyed, so upgrades extract fresh and stale versions are swept) and returns
that path on every later call without touching the overlay again.

Source checkouts use repo-relative data paths.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import threading
from pathlib import Path

from verifier_core import paths

MAGIC = b"ALRVOVL1"
_TAIL_LEN = 88 + len(MAGIC) + 8  # pyinstaller cookie + magic + footer length
_TAIL_SEARCH = 1024 * 1024  # Authenticode may append a certificate after it.

_COPY_CHUNK = 16 * 1024 * 1024

_lock = threading.Lock()
_footer_cache: dict | None = None
_footer_loaded = False


def _exe_path() -> str:
    return sys.executable if getattr(sys, "frozen", False) else ""


def _data_dir() -> Path:
    return paths.data_dir() / "data"


def _read_footer() -> dict | None:
    """Parse and cache the overlay footer, or None when absent."""
    global _footer_cache, _footer_loaded
    if _footer_loaded:
        return _footer_cache
    _footer_loaded = True
    exe = _exe_path()
    if not exe:
        return None
    try:
        size = os.path.getsize(exe)
        if size <= _TAIL_LEN:
            return None
        with open(exe, "rb") as f:
            window_start = max(0, size - _TAIL_SEARCH)
            f.seek(window_start)
            tail = f.read()
            marker = tail.rfind(MAGIC)
            while marker >= 8:
                marker_offset = window_start + marker
                (footer_len,) = struct.unpack("<Q", tail[marker - 8:marker])
                footer_start = marker_offset - 8 - footer_len
                if 0 < footer_len <= marker_offset and footer_start >= 0:
                    try:
                        f.seek(footer_start)
                        candidate = json.loads(f.read(footer_len).decode("utf-8"))
                        entries = candidate.get("entries") if isinstance(candidate, dict) else None
                        if (
                            isinstance(candidate, dict)
                            and candidate.get("version") == 1
                            and isinstance(entries, list)
                            and all(
                                isinstance(entry, dict)
                                and isinstance(entry.get("offset"), int)
                                and isinstance(entry.get("size"), int)
                                and 0 <= entry["offset"] <= footer_start
                                and 0 <= entry["size"] <= footer_start - entry["offset"]
                                for entry in entries
                            )
                        ):
                            footer = candidate
                            break
                    except (OSError, ValueError, UnicodeError):
                        pass
                marker = tail.rfind(MAGIC, 0, marker)
            else:
                return None
    except Exception:
        return None
    _footer_cache = footer
    return footer


def has_overlay() -> bool:
    return _read_footer() is not None


def _entry(name: str) -> dict | None:
    footer = _read_footer()
    if not footer:
        return None
    for entry in footer["entries"]:
        if entry.get("name") == name:
            return entry
    return None


def _target_path(entry: dict) -> Path:
    stem, ext = os.path.splitext(entry["name"])
    return _data_dir() / f"{stem}-{entry['sha256'][:12]}{ext}"


def pending_names() -> list[str]:
    """Overlay entries not yet extracted (empty on every launch but the first)."""
    footer = _read_footer()
    if not footer:
        return []
    return [
        e["name"] for e in footer["entries"]
        if not (_target_path(e).exists() and _target_path(e).stat().st_size == e["size"])
    ]


def _sweep_stale(entry: dict, keep: Path) -> None:
    stem, ext = os.path.splitext(entry["name"])
    try:
        for old in keep.parent.glob(f"{stem}-*{ext}"):
            if old != keep:
                old.unlink(missing_ok=True)
    except Exception:
        pass  # cleanup is best-effort


def _extract(entry: dict, progress=None) -> Path:
    target = _target_path(entry)
    if target.exists() and target.stat().st_size == entry["size"]:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp{os.getpid()}")
    total = entry["size"]
    try:
        with open(_exe_path(), "rb") as src, open(tmp, "wb") as dst:
            src.seek(entry["offset"])
            done = 0
            while done < total:
                chunk = src.read(min(_COPY_CHUNK, total - done))
                if not chunk:
                    raise IOError(f"overlay truncated while extracting {entry['name']}")
                dst.write(chunk)
                done += len(chunk)
                if progress:
                    progress(entry["name"], done, total)
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    _sweep_stale(entry, target)
    return target


def db_path(name: str, progress=None) -> str:
    """Path to the extracted copy of an overlay entry, extracting it first
    if this is the first launch of this build. "" when the exe carries no
    overlay or no such entry (callers fall back to their legacy lookup)."""
    entry = _entry(name)
    if not entry:
        return ""
    with _lock:
        return str(_extract(entry, progress=progress))


def ensure_all(progress=None) -> None:
    """Extract every overlay entry (returns immediately when already extracted)."""
    footer = _read_footer()
    if not footer:
        return
    with _lock:
        for entry in footer["entries"]:
            _extract(entry, progress=progress)
