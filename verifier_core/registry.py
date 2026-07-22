"""Runtime access to the configured US/UK case URL providers."""
from __future__ import annotations

from case_url_providers.composite import CompositeCitationDatabase

from . import api_key_store

api_key_store.apply_saved_provider_keys()
_citation_db = CompositeCitationDatabase()


def get_citation_db() -> CompositeCitationDatabase:
    return _citation_db
