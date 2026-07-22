"""Search public_endpoint.db for journal/article matches by title."""
import difflib
import os
import re
import sqlite3
import sys
import time


DB_FILENAME = "public_endpoint.db"


def _db_path() -> str:
    if getattr(sys, "frozen", False):
        # Frozen builds carry the db appended to the exe; extracted once per
        # build to %LOCALAPPDATA% (never re-copied on later launches).
        try:
            from verifier_core import overlay_store
            path = overlay_store.db_path(DB_FILENAME)
            if path:
                return path
        except Exception:
            pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, DB_FILENAME)
    # From source the reference DB lives under data/.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", DB_FILENAME)


UNFILTERED_SEARCH_TIMEOUT_S = 30.0


_conn: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    # Resolved lazily: on the first frozen launch this may extract the db
    # from the exe overlay, which must not happen at import time. The
    # connection is cached — the db is read-only and reopening it for every
    # candidate row is wasted work. check_same_thread=False because the GUI
    # starts a fresh worker thread per run; access is still serialized (one
    # run at a time).
    global _conn
    if _conn is None:
        con = sqlite3.connect(_db_path(), check_same_thread=False)
        con.row_factory = sqlite3.Row
        _conn = con
    return _conn


def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_DOUBLE_QUOTE_RE = re.compile(r'["\u201c]([^"\u201c\u201d]+?)["\u201d]')
_SINGLE_QUOTE_RE = re.compile(r"['\u2018]([^'\u2018\u2019]+?)['\u2019]")


def _extract_title(verbatim: str) -> str | None:
    """Extract the article title from a journal citation verbatim."""
    titles = _extract_titles(verbatim)
    return titles[0] if titles else None


def _extract_titles(verbatim: str) -> list[str]:
    """Extract all quoted title candidates from a journal citation verbatim."""
    titles: list[str] = []
    seen: set[str] = set()
    for pattern in (_DOUBLE_QUOTE_RE, _SINGLE_QUOTE_RE):
        for m in pattern.finditer(verbatim or ""):
            title = m.group(1).strip()
            norm = _normalize(title)
            if title and norm and norm not in seen:
                seen.add(norm)
                titles.append(title)
    return titles


def search_by_title(verbatim: str, deadline: float | None = None) -> dict | None:
    """Match a journal citation's article title against public_endpoint.db.

    Returns the best match with score >= 0.7, or None.
    """
    titles = _extract_titles(verbatim)
    if not titles:
        return None
    candidates: list[dict] = []
    for title in titles:
        candidates.extend(_search(title, verbatim, top_n=1, deadline=deadline))
        if _deadline_reached(deadline):
            break
    candidates.sort(key=lambda x: x["match_score"], reverse=True)
    if candidates and candidates[0]["match_score"] >= 0.7:
        return candidates[0]
    return None


def extract_title(verbatim: str) -> str | None:
    """Public wrapper around _extract_title for debugging."""
    return _extract_title(verbatim)


def extract_citation_metadata(verbatim: str) -> dict:
    """Extract coarse bibliographic anchors used to narrow journal matching."""
    return _extract_citation_metadata(verbatim)


_JOURNAL_META_RE = re.compile(
    r"""
    [\(\[](?P<year>18\d{2}|19\d{2}|20\d{2})[\)\]]
    \s+
    (?P<volume>\d{1,4})
    (?::(?P<issue>\d{1,3}))?
    \s+
    (?P<journal>.*?)
    \s+
    (?P<first_page>\d{1,5})
    (?:\D|$)
    """,
    re.VERBOSE,
)
_YEAR_RE = re.compile(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2})(?!\d)")


def _extract_citation_metadata(verbatim: str | None) -> dict:
    text = re.sub(r"\s+", " ", (verbatim or "")).strip()
    meta = {"year": None, "journal_hint": "", "volume": None, "issue": None}
    if not text:
        return meta

    m = _JOURNAL_META_RE.search(text)
    if m:
        meta["year"] = _to_int(m.group("year"))
        meta["volume"] = _to_int(m.group("volume"))
        meta["issue"] = _to_int(m.group("issue"))
        journal = (m.group("journal") or "").strip(" ,;:.")
        journal = re.sub(r"\s+", " ", journal)
        if 2 <= len(journal) <= 80:
            meta["journal_hint"] = journal
    else:
        year_match = _YEAR_RE.search(text)
        if year_match:
            meta["year"] = _to_int(year_match.group(1))

    return meta


def _has_metadata_filter(meta: dict) -> bool:
    return bool(
        meta.get("year")
        or meta.get("journal_hint")
        or meta.get("volume")
        or meta.get("issue")
    )


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.perf_counter() >= deadline


def _to_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def search_top_n(verbatim: str, n: int = 5, deadline: float | None = None) -> list[dict]:
    """Return the top N candidate matches for logging/debugging.

    Each candidate has title, journal_name, galley_url, and match_score.
    Always returns top matches regardless of score.
    """
    titles = _extract_titles(verbatim)
    if not titles:
        titles = [verbatim]
    candidates: list[dict] = []
    seen: set[int] = set()
    for title in titles:
        for candidate in _search(title, verbatim, top_n=n, min_score=0.0, loose_filter=True, deadline=deadline):
            article_id = candidate.get("article_id")
            if article_id in seen:
                continue
            seen.add(article_id)
            candidates.append(candidate)
        if _deadline_reached(deadline):
            break
    candidates.sort(key=lambda x: x["match_score"], reverse=True)
    return candidates[:n]


def pdf_page_for_label(article_id: int | str | None, page_label: int | str | None) -> int | None:
    """Return the PDF page for an article page label using public_endpoint page maps."""
    if article_id is None or page_label is None:
        return None
    try:
        article_id_int = int(article_id)
    except (TypeError, ValueError):
        return None
    label = str(page_label).strip()
    if not label:
        return None

    try:
        row = _get_db().execute(
            """
            SELECT pdf_page
            FROM article_pages
            WHERE article_id = ? AND page_label = ?
            ORDER BY page_order
            LIMIT 1
            """,
            (article_id_int, label),
        ).fetchone()
    except Exception:
        return None

    if not row:
        return None
    try:
        return int(row["pdf_page"])
    except (TypeError, ValueError):
        return None


def get_article_text(article_id: int | str | None) -> str:
    """Return full endpoint text for an article, or an empty string if unavailable."""
    if article_id is None:
        return ""
    try:
        article_id_int = int(article_id)
    except (TypeError, ValueError):
        return ""

    try:
        row = _get_db().execute(
            "SELECT text FROM articles WHERE article_id = ? LIMIT 1",
            (article_id_int,),
        ).fetchone()
    except Exception:
        return ""

    return (row["text"] or "") if row else ""


def _search(
    title: str,
    verbatim: str | None = None,
    top_n: int = 1,
    min_score: float = 0.5,
    loose_filter: bool = False,
    deadline: float | None = None,
) -> list[dict]:
    """Fuzzy-match a clean article title against articles.name_en."""
    norm = _normalize(title)
    if not norm:
        return []

    meta = _extract_citation_metadata(verbatim)
    if deadline is None and not _has_metadata_filter(meta):
        deadline = time.perf_counter() + UNFILTERED_SEARCH_TIMEOUT_S

    try:
        rows = _candidate_rows(title, verbatim, loose_filter=loose_filter, metadata=meta)
    except Exception:
        return []

    for row in rows:
        if _deadline_reached(deadline):
            return []
        db_title = row["name_en"] or ""
        if _normalize(db_title) == norm:
            return [_candidate_from_row(row, 1.0)]

    scored: list[dict] = []
    for row in rows:
        if _deadline_reached(deadline):
            break
        db_title = row["name_en"] or ""
        db_norm = _normalize(db_title)
        score = difflib.SequenceMatcher(None, norm, db_norm).ratio()
        if score >= min_score:
            scored.append(_candidate_from_row(row, score))

    scored.sort(key=lambda x: x["match_score"], reverse=True)
    return scored[:top_n]


def _candidate_rows(
    title: str,
    verbatim: str | None,
    *,
    loose_filter: bool,
    metadata: dict | None = None,
) -> list[sqlite3.Row]:
    like_words = _like_words(title)
    metadata = metadata or _extract_citation_metadata(verbatim)

    conditions: list[str] = ["name_en IS NOT NULL"]
    params: list = []
    if like_words:
        if loose_filter:
            word_conditions = " OR ".join(
                "(name_en LIKE ? ESCAPE '\\' OR citation_en LIKE ? ESCAPE '\\')"
                for _ in like_words[:5]
            )
            conditions.append(f"({word_conditions})")
            for word in like_words[:5]:
                params.extend([f"%{_escape_like(word)}%", f"%{_escape_like(word)}%"])
        else:
            for word in like_words[:3]:
                conditions.append("name_en LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(word)}%")

    metadata_conditions, metadata_params = _metadata_sql_conditions(metadata)
    if metadata_conditions:
        conditions.extend(metadata_conditions)
        params.extend(metadata_params)

    sql = f"""
        SELECT
            article_id,
            dataset,
            citation_en,
            name_en,
            journal_name,
            journal_abbrev,
            volume,
            issue,
            first_page,
            last_page,
            galley_url,
            page_export_status
        FROM articles
        WHERE {' AND '.join(conditions)}
    """
    return _get_db().execute(sql, params).fetchall()


def _metadata_sql_conditions(metadata: dict) -> tuple[list[str], list]:
    conditions: list[str] = []
    params: list = []

    year = _to_int(metadata.get("year"))
    if year:
        year_values = [str(y) for y in range(year - 1, year + 2)]
        clauses = []
        for year_value in year_values:
            clauses.append("(document_date_en LIKE ? OR citation_en LIKE ?)")
            params.extend([f"%{year_value}%", f"%{year_value}%"])
        conditions.append("(" + " OR ".join(clauses) + ")")

    journal_hint = (metadata.get("journal_hint") or "").strip()
    if journal_hint:
        escaped = f"%{_escape_like(journal_hint)}%"
        conditions.append(
            "(journal_name LIKE ? ESCAPE '\\' OR journal_abbrev LIKE ? ESCAPE '\\' OR citation_en LIKE ? ESCAPE '\\')"
        )
        params.extend([escaped, escaped, escaped])

    volume = _to_int(metadata.get("volume"))
    if volume:
        conditions.append("(CAST(volume AS INTEGER) BETWEEN ? AND ? OR citation_en LIKE ? ESCAPE '\\')")
        params.extend([max(0, volume - 1), volume + 1, f"%{_escape_like(str(volume))}%"])

    issue = _to_int(metadata.get("issue"))
    if issue:
        conditions.append("(CAST(issue AS INTEGER) BETWEEN ? AND ? OR citation_en LIKE ? ESCAPE '\\')")
        params.extend([max(0, issue - 1), issue + 1, f"%:{_escape_like(str(issue))}%"])

    return conditions, params


def _like_words(title: str) -> list[str]:
    strip_punct = lambda w: w.strip(".,;:!?()[]{}\u201c\u201d\u2018\u2019\"'")
    words = [strip_punct(w) for w in (title or "").lower().split()]
    return [w for w in words if len(w) > 2]


def _candidate_from_row(row: sqlite3.Row, score: float) -> dict:
    return {
        "article_id": row["article_id"],
        "dataset": row["dataset"] or "",
        "title": row["name_en"] or "",
        "citation_en": row["citation_en"] or "",
        "journal_name": row["journal_name"] or "",
        "journal_abbrev": row["journal_abbrev"] or "",
        "volume": row["volume"] or "",
        "issue": row["issue"] or "",
        "first_page": row["first_page"] or "",
        "last_page": row["last_page"] or "",
        "galley_url": row["galley_url"] or "",
        "page_export_status": row["page_export_status"] or "",
        "match_score": score,
    }


def _escape_like(s: str) -> str:
    return s.replace("%", r"\%").replace("_", r"\_")
