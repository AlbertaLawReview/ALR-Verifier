"""Persistent, per-user OpenAI API key storage for end users.

Backend per platform, each the documented convention for desktop apps:

- Windows: DPAPI (CryptProtectData / CryptUnprotectData, user scope) into
  %APPDATA%/ALR Quote Verifier/openai_key.bin. Chosen over the keyring
  package because the app ships as a PyInstaller onefile exe, where
  keyring's backend discovery is known to fail; DPAPI needs no backend
  discovery and no third-party dependency. The ciphertext only decrypts
  for the same Windows user on the same machine.
- macOS: the login Keychain via the `security` CLI (generic password,
  service "ALR Quote Verifier (OpenAI API key)").
- Linux: libsecret via `secret-tool` when available (the secret travels
  over stdin, never argv); otherwise a 0600-permission file under
  ~/.config — the same fallback the GitHub CLI uses when no keyring
  daemon is running.

Never log or display a stored key; use last4() for UI feedback.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from verifier_core import paths

class _SecretSpec:
    def __init__(self, file_name: str, fallback_file_name: str, entropy: bytes,
                 service: str, linux_attrs: tuple):
        self.file_name = file_name
        self.fallback_file_name = fallback_file_name
        self.entropy = entropy
        self.service = service
        self.linux_attrs = linux_attrs


# Entropy is not a secret (it ships in the binary); it just namespaces the
# ciphertext so other DPAPI consumers cannot decrypt our blob by accident.
_SPECS = {
    "openai": _SecretSpec(
        "openai_key.bin", "openai_key.txt",
        b"alr-quote-verifier-openai-key-v1",
        "ALR Quote Verifier (OpenAI API key)",
        ("service", "alr-quote-verifier", "key", "openai-api"),
    ),
    "courtlistener": _SecretSpec(
        "courtlistener_token.bin", "courtlistener_token.txt",
        b"alr-quote-verifier-courtlistener-token-v1",
        "ALR Quote Verifier (CourtListener API token)",
        ("service", "alr-quote-verifier", "key", "courtlistener-api"),
    ),
    "govinfo": _SecretSpec(
        "govinfo_key.bin", "govinfo_key.txt",
        b"alr-quote-verifier-govinfo-key-v1",
        "ALR Quote Verifier (GovInfo API key)",
        ("service", "alr-quote-verifier", "key", "govinfo-api"),
    ),
}

# Env var each optional provider key feeds when no explicit env value is set.
PROVIDER_ENV_VARS = {
    "courtlistener": "COURTLISTENER_API_TOKEN",
    "govinfo": "GOVINFO_API_KEY",
}


# ---------------------------------------------------------------------------
# Windows: DPAPI
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import ctypes.wintypes as wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> _DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _blob_bytes(blob: _DATA_BLOB) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def _dpapi(protect: bool, data: bytes, entropy_bytes: bytes) -> bytes:
        crypt32 = ctypes.windll.crypt32
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        data_in = _blob(data)
        entropy = _blob(entropy_bytes)
        data_out = _DATA_BLOB()
        # CRYPTPROTECT_UI_FORBIDDEN = 0x1 (never pop a DPAPI UI prompt)
        ok = fn(ctypes.byref(data_in), None, ctypes.byref(entropy), None, None, 0x1,
                ctypes.byref(data_out))
        if not ok:
            raise OSError(f"DPAPI {'protect' if protect else 'unprotect'} failed "
                          f"(WinError {ctypes.GetLastError()})")
        return _blob_bytes(data_out)


def _win_key_path(spec: _SecretSpec) -> Path:
    return paths.config_dir() / spec.file_name


def _win_get(spec: _SecretSpec) -> str:
    try:
        blob = _win_key_path(spec).read_bytes()
    except OSError:
        return ""
    try:
        return _dpapi(False, blob, spec.entropy).decode("utf-8")
    except Exception:
        # Wrong user/machine or corrupted file: treat as no stored key.
        return ""


def _win_set(key: str, spec: _SecretSpec) -> None:
    path = _win_key_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi(True, key.encode("utf-8"), spec.entropy))


def _win_clear(spec: _SecretSpec) -> bool:
    try:
        _win_key_path(spec).unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# macOS: login Keychain via the `security` CLI
# ---------------------------------------------------------------------------
def _mac_account() -> str:
    return os.environ.get("USER") or "alr-quote-verifier"


def _mac_get(spec: _SecretSpec) -> str:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", spec.service, "-w"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _mac_set(key: str, spec: _SecretSpec) -> None:
    out = subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", spec.service, "-a", _mac_account(), "-w", key],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        raise OSError(f"Keychain save failed (security exited {out.returncode})")


def _mac_clear(spec: _SecretSpec) -> bool:
    try:
        out = subprocess.run(
            ["security", "delete-generic-password", "-s", spec.service],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return False
    return out.returncode == 0


# ---------------------------------------------------------------------------
# Linux: secret-tool (libsecret) with a 0600-file fallback
# ---------------------------------------------------------------------------
def _fallback_path(spec: _SecretSpec) -> Path:
    return paths.config_dir() / spec.fallback_file_name


def _linux_get(spec: _SecretSpec) -> str:
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", *spec.linux_attrs],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        return _fallback_path(spec).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _linux_set(key: str, spec: _SecretSpec) -> None:
    try:
        out = subprocess.run(
            ["secret-tool", "store", "--label", spec.service, *spec.linux_attrs],
            input=key, capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return
    except Exception:
        pass
    path = _fallback_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    os.chmod(path, 0o600)


def _linux_clear(spec: _SecretSpec) -> bool:
    removed = False
    try:
        out = subprocess.run(
            ["secret-tool", "clear", *spec.linux_attrs],
            capture_output=True, text=True, timeout=15,
        )
        removed = out.returncode == 0
    except Exception:
        pass
    try:
        _fallback_path(spec).unlink()
        removed = True
    except OSError:
        pass
    return removed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_key(name: str = "openai") -> str:
    """Return the stored key, or "" when absent/unreadable."""
    spec = _SPECS[name]
    if sys.platform == "win32":
        return _win_get(spec)
    if sys.platform == "darwin":
        return _mac_get(spec)
    return _linux_get(spec)


def set_key(key: str, name: str = "openai") -> None:
    """Persist the key for the current OS user."""
    key = (key or "").strip()
    if not key:
        raise ValueError("empty API key")
    spec = _SPECS[name]
    if sys.platform == "win32":
        _win_set(key, spec)
    elif sys.platform == "darwin":
        _mac_set(key, spec)
    else:
        _linux_set(key, spec)


def clear_key(name: str = "openai") -> bool:
    """Delete the stored key. Returns True when something was removed."""
    spec = _SPECS[name]
    if sys.platform == "win32":
        return _win_clear(spec)
    if sys.platform == "darwin":
        return _mac_clear(spec)
    return _linux_clear(spec)


def has_key(name: str = "openai") -> bool:
    return bool(get_key(name))


def apply_saved_provider_keys() -> None:
    """Feed stored optional provider keys into the environment the web
    providers read, without overriding explicit env configuration."""
    for name, env_var in PROVIDER_ENV_VARS.items():
        if os.environ.get(env_var):
            continue
        try:
            value = get_key(name)
        except Exception:
            continue
        if value:
            os.environ[env_var] = value


def last4(key: str) -> str:
    """Display form for UI feedback: ellipsis + last 4 characters."""
    key = (key or "").strip()
    return f"…{key[-4:]}" if len(key) >= 8 else ""
