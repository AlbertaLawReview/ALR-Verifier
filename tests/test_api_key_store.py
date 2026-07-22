"""Round-trip tests for the DPAPI-backed per-user key store (Windows-only)."""
import sys

import pytest

from verifier_core import api_key_store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


@pytest.fixture()
def isolated_appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def test_round_trip(isolated_appdata):
    assert api_key_store.get_key() == ""
    api_key_store.set_key("sk-test-1234567890ABCD")
    assert api_key_store.get_key() == "sk-test-1234567890ABCD"
    assert api_key_store.has_key()
    # ciphertext on disk, not plaintext
    blob = (isolated_appdata / "ALR Quote Verifier" / "openai_key.bin").read_bytes()
    assert b"sk-test-1234567890ABCD" not in blob


def test_clear(isolated_appdata):
    api_key_store.set_key("sk-test-1234567890ABCD")
    assert api_key_store.clear_key() is True
    assert api_key_store.get_key() == ""
    assert api_key_store.clear_key() is False


def test_corrupt_blob_reads_as_missing(isolated_appdata):
    path = isolated_appdata / "ALR Quote Verifier" / "openai_key.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a dpapi blob")
    assert api_key_store.get_key() == ""


def test_empty_key_rejected(isolated_appdata):
    with pytest.raises(ValueError):
        api_key_store.set_key("   ")


def test_last4_display():
    assert api_key_store.last4("sk-abcdefgh1234") == "…1234"
    assert api_key_store.last4("short") == ""
    assert api_key_store.last4("") == ""
