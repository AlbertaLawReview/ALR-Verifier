"""Neutral legal-PDF runtime pinned to upstream commit 51d51c4.

Only the deterministic parsing modules needed by ALR Quote Verifier are
included here; model repair, command-line, benchmark, and DOCX tooling remain
in the independently owned universal engine.
"""

from .core import parse_pdf

__all__ = ["parse_pdf"]
