"""The vendored PDF engine must be able to hash itself inside the frozen exe.

legalpdf_engine.core._engine_identity() stamps provenance on every parse by
reading its own source files off disk. PyInstaller archives compiled modules,
not .py sources, so unless packaging ships those sources as data files the
first PDF opened in the built app dies on

    FileNotFoundError: ...\\_MEI******\\verifier_core\\legalpdf_engine\\core.py

which is what shipped in v1.05. Nothing in the test suite caught it, because
every test runs from the source tree where the .py files are trivially
present. These tests assert the packaging contract instead.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "verifier_core" / "legalpdf_engine"
BUILD_EXE = ROOT / "packaging" / "build_exe.py"


def _identity_filenames() -> set[str]:
    """Every path literal _engine_identity() hands to _sha256_file.

    Read out of the source with ast rather than by calling the function, so
    this still describes the contract when the engine is re-vendored.
    """
    tree = ast.parse((ENGINE_DIR / "core.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_engine_identity":
            return {
                literal.value
                for literal in ast.walk(node)
                if isinstance(literal, ast.Constant)
                and isinstance(literal.value, str)
                and literal.value.endswith(".py")
            }
    raise AssertionError("_engine_identity() not found in legalpdf_engine/core.py")


def test_every_source_the_engine_hashes_exists():
    missing = sorted(n for n in _identity_filenames() if not (ENGINE_DIR / n).is_file())
    assert not missing, f"_engine_identity() hashes files not in the vendored copy: {missing}"


@pytest.mark.skipif(not BUILD_EXE.is_file(), reason="internal-edition build config only")
def test_build_ships_the_engine_sources_as_data():
    """build_exe must copy the engine's .py sources into the bundle.

    Checked as text: importing build_exe.py runs an entire PyInstaller build.
    """
    source = BUILD_EXE.read_text(encoding="utf-8")
    block = re.search(
        r"_ENGINE_DIR\s*=\s*os\.path\.join\(.*?\)\s*\ndatas\s*\+=\s*\[(.*?)\]",
        source,
        re.DOTALL,
    )
    assert block, (
        "packaging/build_exe.py no longer ships verifier_core/legalpdf_engine "
        "sources as data. _engine_identity() reads them off disk at runtime, so "
        "without them every PDF fails in the built exe."
    )
    rule = block.group(1)
    assert '.endswith(".py")' in rule, "the datas rule must cover the engine's .py sources"
    assert "VENDORED.json" in rule, "the vendored manifest must ship beside the sources"


def test_engine_sources_are_tracked_so_a_fresh_clone_can_build():
    """ALR carries its own copy of the engine; it is not a local checkout.

    A contributor cloning this repo must get the engine, so every source the
    identity stamp covers has to be a real file in the tree (see VENDORED.json
    for the hashes each one is pinned to).
    """
    manifest_files = set(
        __import__("json").loads((ENGINE_DIR / "VENDORED.json").read_text(encoding="utf-8"))["files"]
    )
    for name in _identity_filenames():
        assert name in manifest_files, (
            f"{name} is hashed into engine identity but is not pinned in "
            "VENDORED.json, so a re-vendor could silently change it"
        )
