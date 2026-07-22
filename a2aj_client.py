"""A2AJ API client for fetching source text by citation.

Public API at https://api.a2aj.ca — no key required.
Queried before browser-based source retrieval for supported citations.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional

from local_a2aj import LocalA2AJCorpus

A2AJ_BASE_URL = "https://api.a2aj.ca"
A2AJ_CACHE_DIR = os.path.join("cache", "a2aj")
A2AJ_REPORTER_ALIASES_PATH = os.path.join(os.path.dirname(__file__), "data", "a2aj_reporter_aliases.json")
A2AJ_MIN_SECONDS_BETWEEN_REQUESTS = 1.0
A2AJ_REQUEST_TIMEOUT_SECONDS = 30
A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS = 86400
A2AJ_LOOKUP_CACHE_MAX_ENTRIES = 512


def _http_get(*args: Any, **kwargs: Any) -> Any:
    import requests

    return requests.get(*args, **kwargs)


@dataclass(frozen=True)
class A2AJDocument:
    dataset: str
    citation: str
    alternate_citation: str
    name: str
    date: str
    url: str
    text: str
    language: str
    scraped_timestamp: str
    upstream_license: str
    raw: Dict[str, Any]


@dataclass(frozen=True)
class A2AJLookup:
    status: str
    document: Optional[A2AJDocument] = None
    method: str = ""


_NUM_DOT_RE = re.compile(r"(?<=\d)\.(?=\d)")
_NUM_DASH_RE = re.compile(r"(?<=\d)-(?=\d)")
_NUM_SLASH_RE = re.compile(r"(?<=\d)/(?=\d)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_PINPOINT_TAIL_RE = re.compile(
    r"\s+at\s+(?:(?:pp?|paras?|(?:sub)?sections?|ss?|rules?|rr?|arts?|articles?)"
    r"\.?\s*)?\d.*$",
    re.I,
)
_PROVISION_TAIL_RE = re.compile(
    r",\s*(?:(?:sub)?sections?|ss?\.?|rules?|rr?\.?|arts?\.?|articles?)"
    r"\s+\d[\w().,\s-]*$",
    re.I,
)
_CANONICAL_PROVISION_SUFFIX_RE = re.compile(
    r",\s*(?P<label>rr?|rules?|s(?:ection)?s?|arts?|articles?)\.?\s*"
    r"(?P<number>\d{1,8}(?:[.-]\d{1,8}){0,3}(?:\([^)]+\))*)\s*$",
    re.I,
)


def _citation_literal_key(value: str) -> str:
    """Exact citation identity without discarding a canonical rule suffix."""
    value = unicodedata.normalize("NFKC", str(value or "")).replace("\u2013", "-").replace("\u2014", "-")
    value = _NUM_DOT_RE.sub("dot", value)
    value = _NUM_DASH_RE.sub("dash", value)
    value = _NUM_SLASH_RE.sub("slash", value)
    return _NON_ALNUM_RE.sub("", value.lower())


def _citation_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).replace("\u2013", "-").replace("\u2014", "-")
    value = _PINPOINT_TAIL_RE.sub("", value)
    value = _PROVISION_TAIL_RE.sub("", value)
    return _NON_ALNUM_RE.sub("", value.lower())


def _canonical_provision_suffix(value: str) -> str:
    match = _CANONICAL_PROVISION_SUFFIX_RE.search(str(value or ""))
    if not match:
        return ""
    label = match.group("label").casefold()
    family = "r" if label.startswith("r") else "s" if label.startswith("s") else "art"
    return family + ":" + _citation_literal_key(match.group("number"))


def _document(obj: Dict[str, Any], language: str) -> A2AJDocument:
    lang = language if language in ("en", "fr") else "en"
    if not obj.get(f"unofficial_text_{lang}"):
        lang = "fr" if lang == "en" and obj.get("unofficial_text_fr") else "en"
    return A2AJDocument(
        dataset=str(obj.get("dataset") or ""),
        citation=str(obj.get(f"citation_{lang}") or obj.get("citation_en") or obj.get("citation_fr") or ""),
        alternate_citation=str(obj.get(f"citation2_{lang}") or ""),
        name=str(obj.get(f"name_{lang}") or obj.get("name_en") or obj.get("name_fr") or ""),
        date=str(obj.get(f"document_date_{lang}") or ""),
        # Corpus/API law rows expose ``source_url_*`` (the official
        # government source), while older case fixtures used ``url_*``.
        url=str(obj.get(f"source_url_{lang}") or obj.get(f"url_{lang}") or ""),
        text=str(obj.get(f"unofficial_text_{lang}") or ""),
        language=lang,
        scraped_timestamp=str(obj.get(f"scraped_timestamp_{lang}") or ""),
        upstream_license=str(obj.get("upstream_license") or ""),
        raw=obj,
    )


def _extract_text_field(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("text", "full_text", "document_text", "content", "body"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for k in ("en", "fr", "english", "french"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):
                vv = _extract_text_field(v)
                if vv:
                    return vv
        for k, v in obj.items():
            if "text" in str(k).lower() and isinstance(v, str) and v.strip():
                return v
        return ""
    if isinstance(obj, list):
        for item in obj:
            t = _extract_text_field(item)
            if t and not t.lstrip().startswith("{"):
                return t
        return _extract_text_field(obj[0]) if obj else ""
    return str(obj)


class A2AJClient:
    def __init__(
        self,
        base_url: str = A2AJ_BASE_URL,
        cache_dir: Optional[str] = None,
        reporter_aliases_path: Optional[str] = None,
        min_seconds_between_requests: float = A2AJ_MIN_SECONDS_BETWEEN_REQUESTS,
        timeout_seconds: int = A2AJ_REQUEST_TIMEOUT_SECONDS,
        local_corpus: Optional[LocalA2AJCorpus] = None,
        local_only: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir or A2AJ_CACHE_DIR
        self.min_wait = float(min_seconds_between_requests or 0.0)
        self.timeout = int(timeout_seconds)
        self.reporter_aliases_path = (
            A2AJ_REPORTER_ALIASES_PATH if reporter_aliases_path is None else reporter_aliases_path
        )
        self.local_corpus = local_corpus
        self.local_only = bool(local_only)
        self._last_request_ts = 0.0
        self._lookup_cache: Dict[tuple[Any, ...], tuple[A2AJLookup, float]] = {}
        self._coverage_cache: Dict[str, tuple[set[str], float]] = {}
        self._reporter_aliases: Optional[Dict[str, str]] = None

        os.makedirs(self.cache_dir, exist_ok=True)

    def _cache_key(self, path: str, params: Dict[str, Any]) -> str:
        payload = json.dumps({"path": path, "params": params}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _cache_paths(self, key: str) -> tuple[str, str]:
        return (
            os.path.join(self.cache_dir, f"{key}.json"),
            os.path.join(self.cache_dir, f"{key}.meta.json"),
        )

    def _maybe_sleep(self):
        if self.min_wait <= 0:
            return
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < self.min_wait:
            time.sleep(self.min_wait - elapsed)

    def clear_memory_cache(self) -> None:
        self._lookup_cache.clear()
        self._coverage_cache.clear()

    def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        local = self._get_local(path, params)
        if local is not None and (local.get("json") or {}).get("results"):
            return local
        if self.local_only:
            return local or {
                "http_status": None, "json": None,
                "text": "LOCAL_ONLY: the required A2AJ corpus is not installed",
            }

        url = f"{self.base_url}{path}"
        params = {k: v for k, v in params.items() if v is not None}

        key = self._cache_key(path, params)
        cache_path, meta_path = self._cache_paths(key)
        negative_path = f"{cache_path}.negative"
        stale: Optional[Dict[str, Any]] = None
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            permanent_case = (
                path == "/fetch"
                and str(params.get("doc_type") or "").lower() in {"case", "cases"}
            )
            if permanent_case or time.time() - os.path.getmtime(cache_path) < A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS:
                return cached
            stale = cached
        if os.path.exists(negative_path) and time.time() - os.path.getmtime(negative_path) < 86400:
            if stale:
                return stale
            with open(negative_path, "r", encoding="utf-8") as f:
                return json.load(f)

        self._maybe_sleep()

        try:
            r = _http_get(url, params=params, timeout=self.timeout)
            self._last_request_ts = time.time()
            out: Dict[str, Any] = {"http_status": r.status_code, "json": None, "text": None}
            try:
                out["json"] = r.json()
            except Exception:
                out["text"] = r.text[:5000]

            # Only cache successful responses with actual data
            if (out.get("http_status") == 200
                    and out.get("json") is not None
                    and isinstance(out["json"], dict)
                    and out["json"].get("results")):
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump({"url": url, "params": params}, f, ensure_ascii=False, indent=2)
            elif out.get("http_status") == 200 and isinstance(out.get("json"), dict) and not out["json"].get("results"):
                with open(negative_path, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False)

            if stale and not (
                out.get("http_status") == 200
                and isinstance(out.get("json"), dict)
                and out["json"].get("results")
            ):
                return stale
            return out
        except Exception as e:
            self._last_request_ts = time.time()
            if stale:
                return stale
            return {"http_status": None, "json": None, "text": f"REQUEST_ERROR: {e}"}

    def _get_local(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        corpus = self.local_corpus
        if corpus is None:
            return None
        doc_type = str(params.get("doc_type") or "")
        try:
            if path == "/coverage":
                datasets = corpus.coverage(doc_type)
                if datasets:
                    return {
                        "http_status": 200,
                        "json": {"results": [{"dataset": item} for item in sorted(datasets)]},
                        "text": None,
                        "local": True,
                    }
            elif path == "/fetch":
                return corpus.fetch(
                    str(params.get("citation") or ""), doc_type,
                    section=str(params.get("section") or ""),
                    output_language=str(params.get("output_language") or "en"),
                )
            elif path == "/search":
                query = str(params.get("query") or "")
                citation = corpus.fetch(query, doc_type)
                if (citation.get("json") or {}).get("results"):
                    return citation
                return corpus.search_exact_name(query, doc_type)
        except (OSError, RuntimeError, ValueError):
            pass
        return None

    def fetch(self, citation: str, doc_type: str, *, section: str = "", output_language: str = "en") -> Dict[str, Any]:
        return self.get(
            "/fetch",
            {
                "citation": citation,
                "doc_type": doc_type,
                "section": section or "",
                "output_language": output_language,
            },
        )

    def _reporter_alias(self, citation: str) -> str:
        if self._reporter_aliases is None:
            self._reporter_aliases = {}
            if self.reporter_aliases_path and os.path.exists(self.reporter_aliases_path):
                try:
                    with open(self.reporter_aliases_path, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                    self._reporter_aliases = {
                        str(key): str(value.get("canonical_citation") or "")
                        for key, value in (payload.get("aliases") or {}).items()
                        if isinstance(value, dict) and value.get("canonical_citation")
                    }
                except (OSError, ValueError, TypeError):
                    pass
        if not self._reporter_aliases:
            return ""
        hit = self._reporter_aliases.get(_citation_key(citation), "")
        if hit:
            return hit
        # Footnotes attach case names and parallel citations ahead of the
        # reporter citation ("Hunter et al v Southam Inc, 11 DLR (4th) 641");
        # retry each comma-delimited tail so the citation itself can match.
        parts = str(citation or "").split(",")
        for start in range(1, len(parts)):
            key = _citation_key(",".join(parts[start:]))
            if key:
                hit = self._reporter_aliases.get(key, "")
                if hit:
                    return hit
        return ""

    def reporter_alias_canonical(self, citation: str) -> str:
        """Consensus canonical citation for a reporter-alias citation, or ""."""
        return self._reporter_alias(citation)

    def _lookup_exact(
        self, citation: str, doc_type: str, *, section: str, output_language: str, search: bool
    ) -> A2AJLookup:
        result = self.fetch(citation, doc_type, section=section, output_language=output_language)
        if result.get("http_status") is None:
            return A2AJLookup("network_error")
        payload = result.get("json") or {}
        candidates = payload.get("results") or [] if isinstance(payload, dict) else []
        literal_key = _citation_literal_key(citation)
        key = _citation_key(citation)
        query_suffix = _canonical_provision_suffix(citation) if doc_type in {"law", "laws"} else ""

        def broad_identity_match(obj: Any) -> bool:
            if not isinstance(obj, dict) or not key:
                return False
            for field in ("citation_en", "citation2_en", "citation_fr", "citation2_fr"):
                candidate = obj.get(field)
                if _citation_key(candidate) != key:
                    continue
                candidate_suffix = _canonical_provision_suffix(candidate)
                if query_suffix and candidate_suffix and candidate_suffix != query_suffix:
                    continue
                return True
            return False

        def identity_matches(items: Any) -> list[Dict[str, Any]]:
            literal_exact = [obj for obj in items if isinstance(obj, dict) and literal_key and literal_key in {
                _citation_literal_key(obj.get("citation_en")), _citation_literal_key(obj.get("citation2_en")),
                _citation_literal_key(obj.get("citation_fr")), _citation_literal_key(obj.get("citation2_fr")),
            }]
            return literal_exact or [obj for obj in items if broad_identity_match(obj)]

        exact = identity_matches(candidates)
        needs_bilingual = not exact or not any(
            obj.get(f"unofficial_text_{output_language}")
            for obj in exact if isinstance(obj, dict)
        )
        if needs_bilingual and doc_type in {"law", "laws"}:
            bilingual = self.fetch(citation, doc_type, section=section, output_language="both")
            bilingual_payload = bilingual.get("json") or {}
            bilingual_candidates = (
                bilingual_payload.get("results") or []
                if isinstance(bilingual_payload, dict) else []
            )
            bilingual_exact = identity_matches(bilingual_candidates)
            if bilingual_exact:
                result = bilingual
                exact = bilingual_exact
        if not exact and search:
            searched = self.get("/search", {
                "query": citation, "search_type": "full_text", "doc_type": doc_type,
                "search_language": output_language, "size": 10,
            })
            search_payload = searched.get("json") or {}
            search_results = search_payload.get("results") or [] if isinstance(search_payload, dict) else []
            search_exact = identity_matches(search_results)
            if len(search_exact) != 1:
                return A2AJLookup("ambiguous" if len(search_exact) > 1 else "not_found")
            identity = next(
                (
                    str(search_exact[0].get(field) or "").strip()
                    for field in ("citation_en", "citation_fr", "citation2_en", "citation2_fr")
                    if str(search_exact[0].get(field) or "").strip()
                ),
                "",
            )
            if not identity:
                return A2AJLookup("not_found")
            return self._lookup_exact(
                identity,
                doc_type,
                section=section,
                output_language=output_language,
                search=False,
            )
        if len(exact) != 1:
            return A2AJLookup("ambiguous" if len(exact) > 1 else "not_found")
        selected = dict(exact[0])
        if result.get("local"):
            selected_keys = {
                _citation_literal_key(selected.get(field))
                for field in ("citation_en", "citation2_en", "citation_fr", "citation2_fr")
                if selected.get(field)
            }
            for raw in result.get("_local_raw_results") or ():
                if not isinstance(raw, dict):
                    continue
                raw_keys = {
                    _citation_literal_key(raw.get(field))
                    for field in ("citation_en", "citation2_en", "citation_fr", "citation2_fr")
                    if raw.get(field)
                }
                if selected_keys & raw_keys:
                    for field in ("unofficial_sections_en", "unofficial_sections_fr"):
                        if field in raw:
                            selected[field] = raw[field]
                    break
        document = _document(selected, output_language)
        for language in ("en", "fr"):
            if language != document.language:
                selected.pop(f"unofficial_text_{language}", None)
                selected.pop(f"unofficial_sections_{language}", None)
        return A2AJLookup("found", document, "exact_citation")

    def lookup(
        self, citation: str, doc_type: str, *, section: str = "", language: str = "en", search: bool = True
    ) -> A2AJLookup:
        """Return one exact-citation document; approximate/name matches never lock identity."""
        output_language = language if language in ("en", "fr") else "en"
        cache_key = (
            doc_type,
            _citation_literal_key(citation),
            _citation_key(citation),
            section,
            output_language,
            search,
        )
        cached = self._lookup_cache.get(cache_key)
        if cached and (
            (doc_type in {"case", "cases"} and cached[0].status == "found")
            or time.time() - cached[1] < A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS
        ):
            return cached[0]
        lookup = self._lookup_exact(
            citation, doc_type, section=section, output_language=output_language, search=search
        )
        if lookup.status == "not_found" and doc_type == "cases":
            canonical = self._reporter_alias(citation)
            if canonical and _citation_literal_key(canonical) != _citation_literal_key(citation):
                aliased = self._lookup_exact(
                    canonical, doc_type, section=section,
                    output_language=output_language, search=search,
                )
                if aliased.status == "found":
                    lookup = A2AJLookup("found", aliased.document, "consensus_reporter_alias")
        self._lookup_cache[cache_key] = (lookup, time.time())
        if len(self._lookup_cache) > A2AJ_LOOKUP_CACHE_MAX_ENTRIES:
            self._lookup_cache.pop(next(iter(self._lookup_cache)))
        return lookup

    def coverage(self, doc_type: str) -> set[str]:
        cached = self._coverage_cache.get(doc_type)
        if cached and time.time() - cached[1] < A2AJ_MUTABLE_CACHE_MAX_AGE_SECONDS:
            return cached[0]
        result = self.get("/coverage", {"doc_type": doc_type})
        payload = result.get("json") or {}
        datasets = {
            str(item.get("dataset") or "").upper()
            for item in (payload.get("results") or [] if isinstance(payload, dict) else [])
            if isinstance(item, dict) and item.get("dataset")
        }
        if datasets:
            self._coverage_cache[doc_type] = (datasets, time.time())
        return datasets

    def fetch_text(self, citation: str, doc_type: str) -> str:
        """Fetch the full source text for a citation. Returns empty string on failure."""
        result = self.fetch(citation, doc_type)
        if result.get("http_status") != 200 or not result.get("json"):
            return ""
        payload = result["json"]
        results = payload.get("results") if isinstance(payload, dict) else None
        if not results:
            return ""
        doc_obj = results[0]
        return _extract_text_field(doc_obj)


_local_corpus = LocalA2AJCorpus()
_local_only = False
_a2aj_client: Optional[A2AJClient] = None


def get_local_corpus() -> LocalA2AJCorpus:
    return _local_corpus


def set_local_only(enabled: bool) -> None:
    global _local_only
    changed = _local_only != bool(enabled)
    _local_only = bool(enabled)
    if _a2aj_client is not None:
        _a2aj_client.local_only = _local_only
        if changed:
            _a2aj_client.clear_memory_cache()


def clear_memory_cache() -> None:
    if _a2aj_client is not None:
        _a2aj_client.clear_memory_cache()

def get_client() -> A2AJClient:
    global _a2aj_client
    if _a2aj_client is None:
        _a2aj_client = A2AJClient(
            local_corpus=_local_corpus, local_only=_local_only
        )
    return _a2aj_client


def fetch_source_text(citation: str, kind: str) -> str:
    """Fetch source text from A2AJ given a citation and GPT kind.

    Maps kind to A2AJ doc_type:
      - case/unreported -> "cases"
      - statute/gazette  -> "laws"
      - everything else  -> returns ""
    """
    if kind in ("case", "unreported"):
        doc_type = "cases"
    elif kind in ("statute", "gazette"):
        doc_type = "laws"
    else:
        return ""

    client = get_client()
    return client.fetch_text(citation, doc_type)


def lookup_document(
    citation: str, kind: str, *, section: str = "", language: str = "en", search: bool = True
) -> A2AJLookup:
    if kind in ("case", "unreported", "cases"):
        doc_type = "cases"
    elif kind in ("statute", "gazette", "laws"):
        doc_type = "laws"
    else:
        return A2AJLookup("outside_coverage")
    return get_client().lookup(citation, doc_type, section=section, language=language, search=search)
