"""URL handling used by source lookups."""
from __future__ import annotations

from typing import Any


def _ts_print(msg: str) -> None:
    try:
        import alr_quote_verifier as _app
        _app._ts_print(msg)
    except Exception:
        print(msg, flush=True)


class UrlResolver:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def resolve_url(self, url: str) -> str:
        return (url or '').strip()

    def close(self) -> None:
        pass
