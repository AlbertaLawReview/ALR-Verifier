"""Guard against the UTF-8/cp1252 round trip that has now corrupted two files.

`gui.py` shipped a status line whose ellipsis had turned into three garbage
characters, `verifier_core/a2aj_structure.py` carried seven mojibaked regex
literals that compiled fine and silently never matched, and a workbook column
header in `alr_quote_verifier.py` lost its arrow and with it a width lookup.
All the same accident: UTF-8 bytes read as cp1252 and written back out as
UTF-8. It survives review, and inside a regex it survives runtime too — so
check for it mechanically rather than by eye.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# What a UTF-8 lead byte looks like once it has been misread as cp1252.
LEADS = "ÂÃàáâãäåæçèéêë"
# The characters cp1252 assigns to 0x80-0xFF, which is what every byte of a
# misread sequence must decode to for the round trip to be reversible.
CP1252_UPPER = {
    bytes([code]).decode("cp1252") for code in range(0x80, 0x100)
    if bytes([code]).decode("cp1252", "ignore")
}

# Generated, vendored or third-party trees. dist_source is regenerated from
# the sources this test does cover; data/ and cache/ hold provider payloads
# whose mojibake arrived from upstream and is not ours to rewrite.
SKIP = ("packaging/dist_source/", "packaging/build/", "packaging/dist/",
        "_temp/", "cache/", "data/", "CHECKED_EDITS/")


def mojibake(text: str):
    """Yield ``(index, as_written, intended)`` for each reversible corruption.

    A run only counts when every character in it is one cp1252 assigns to the
    upper half *and* the whole run decodes back to exactly one character. That
    is what keeps ordinary French out of the results: "câble" starts with a
    lead character too, but "b" is ASCII, so the run never forms.
    """
    index = 0
    while index < len(text):
        if text[index] in LEADS:
            for length in (4, 3, 2):
                run = text[index:index + length]
                if len(run) < length or any(c not in CP1252_UPPER for c in run):
                    continue
                try:
                    intended = run.encode("cp1252").decode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    continue
                if len(intended) == 1:
                    yield index, run, intended
                    index += length
                    break
            else:
                index += 1
        else:
            index += 1


def repair(text: str) -> str:
    out = []
    position = 0
    for index, run, intended in mojibake(text):
        out.append(text[position:index])
        out.append(intended)
        position = index + len(run)
    out.append(text[position:])
    return "".join(out)


def _tracked_python_files() -> list[Path]:
    """Tracked sources plus new ones, so a file is checked before it lands."""
    names: set[str] = set()
    for extra in ([], ["--others", "--exclude-standard"]):
        listing = subprocess.run(
            ["git", "ls-files", "-z", *extra, "*.py"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        names.update(name for name in listing.split("\0") if name)
    return [
        ROOT / name for name in sorted(names)
        if not any(name.startswith(prefix) for prefix in SKIP)
    ]


def test_no_source_file_carries_a_cp1252_round_trip():
    found = []
    for path in _tracked_python_files():
        text = path.read_text(encoding="utf-8")
        for index, run, intended in mojibake(text):
            line = text.count("\n", 0, index) + 1
            found.append(
                f"{path.relative_to(ROOT).as_posix()}:{line} "
                f"{run!r} should be {intended!r}"
            )
    assert not found, "mojibake in tracked sources:\n  " + "\n  ".join(found)


def corrupt(text: str) -> str:
    """Do to `text` exactly what the accident did: write UTF-8, read cp1252."""
    return text.encode("utf-8").decode("cp1252")


# Derived rather than written out, so this file stays clean enough to pass its
# own scan above. Each is a string that really was corrupted in this repo.
@pytest.mark.parametrize("intended", [
    "corpus…",             # gui.py shipped this to the status bar
    "abrogées",            # a2aj_structure.py
    "[-–—]",               # two adjacent runs
    "préambule",
    "[\"'“«]",
    "Automatic  Checking  System ►",   # alr_quote_verifier.py, a column header
])
def test_the_guard_recognises_the_corruption_it_is_written_for(intended):
    written = corrupt(intended)
    assert written != intended, "fixture must actually be corrupted"
    assert list(mojibake(written)), f"{written!r} should have been flagged"
    assert repair(written) == intended


@pytest.mark.parametrize("text", [
    "un câble",           # French circumflex before an ASCII letter
    "à la fois",          # a grave
    "abrogées",           # the repaired spellings must not be flagged again
    "règlements",
    "préambule",
    "“Interpretation” — 5–7",
])
def test_the_guard_leaves_correctly_encoded_text_alone(text):
    assert not list(mojibake(text))
    assert repair(text) == text
