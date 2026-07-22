"""Round-trip test for the exe overlay format (build_exe appends it; the
frozen app reads it back via verifier_core.overlay_store)."""
import hashlib
import json
import os
import struct

import pytest

from verifier_core import overlay_store


def _make_fake_exe(path, entries):
    """Write [junk][entry bytes...][footer][u64 len][magic][88B cookie]."""
    blobs = {name: data for name, data in entries}
    with open(path, "wb") as f:
        f.write(os.urandom(4096))  # stand-in for the real exe bytes
        footer_entries = []
        for name, data in entries:
            footer_entries.append({
                "name": name,
                "offset": f.tell(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            f.write(data)
        footer = json.dumps({"version": 1, "entries": footer_entries}).encode()
        f.write(footer)
        f.write(struct.pack("<Q", len(footer)))
        f.write(overlay_store.MAGIC)
        f.write(b"\0" * 88)  # bootloader cookie: opaque to overlay_store
    return blobs


@pytest.fixture
def fake_frozen(tmp_path, monkeypatch):
    """Point overlay_store at a fake exe + fake %LOCALAPPDATA% data dir."""
    exe = tmp_path / "fake.exe"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(overlay_store, "_exe_path", lambda: str(exe))
    monkeypatch.setattr(overlay_store, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(overlay_store, "_footer_cache", None)
    monkeypatch.setattr(overlay_store, "_footer_loaded", False)
    return exe, data_dir


def test_no_overlay_is_graceful(fake_frozen):
    exe, _ = fake_frozen
    exe.write_bytes(os.urandom(2048))  # plain exe, no overlay tail
    assert not overlay_store.has_overlay()
    assert overlay_store.pending_names() == []
    assert overlay_store.db_path("public_endpoint.db") == ""


def test_extract_once_and_reuse(fake_frozen):
    exe, data_dir = fake_frozen
    payload = os.urandom(300_000)
    _make_fake_exe(exe, [("public_endpoint.db", payload), ("extra.db", b"tiny")])

    assert overlay_store.has_overlay()
    assert set(overlay_store.pending_names()) == {"public_endpoint.db", "extra.db"}

    path = overlay_store.db_path("public_endpoint.db")
    assert path and open(path, "rb").read() == payload
    assert overlay_store.pending_names() == ["extra.db"]

    # Second resolve must reuse the extracted copy, not re-copy it.
    before = os.stat(path).st_mtime_ns
    assert overlay_store.db_path("public_endpoint.db") == path
    assert os.stat(path).st_mtime_ns == before

    overlay_store.ensure_all()
    assert overlay_store.pending_names() == []
    assert overlay_store.db_path("missing.db") == ""


def test_overlay_survives_authenticode_trailer(fake_frozen):
    exe, _ = fake_frozen
    payload = os.urandom(300_000)
    _make_fake_exe(exe, [("public_endpoint.db", payload)])
    with open(exe, "ab") as f:
        f.write(os.urandom(16_384))  # stand-in for an appended certificate

    assert overlay_store.has_overlay()
    path = overlay_store.db_path("public_endpoint.db")
    assert open(path, "rb").read() == payload


def test_stale_versions_swept(fake_frozen):
    exe, data_dir = fake_frozen
    _make_fake_exe(exe, [("public_endpoint.db", b"new contents")])
    data_dir.mkdir(parents=True)
    stale = data_dir / "public_endpoint-deadbeef0000.db"
    stale.write_bytes(b"old build's copy")

    path = overlay_store.db_path("public_endpoint.db")
    assert open(path, "rb").read() == b"new contents"
    assert not stale.exists()
