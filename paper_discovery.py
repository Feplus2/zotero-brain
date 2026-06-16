# -*- coding: utf-8 -*-
"""
Paper Discovery - search academic databases for papers.

Sources (by priority):
  - OpenAlex API (https://api.openalex.org) - primary, 240M+ papers, free, no key, OA URL
  - CrossRef API (https://api.crossref.org) - DOI metadata
  - arXiv API (https://export.arxiv.org/api) - preprints
  - Semantic Scholar API (https://api.semanticscholar.org) - fallback, key/rate-limited

Unified return format:
  {
    "title": "...",
    "authors": ["Last First", ...],
    "year": 2025,
    "doi": "10.xxx/...",
    "abstract": "...",
    "citation_count": 42,
    "open_access_pdf": "https://..." or None,
    "source": "openalex" | "semantic_scholar" | "arxiv" | "crossref",
    "url": "https://...",
  }

Dependencies: httpx
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict

import httpx

import config
import vector_store

logger = logging.getLogger(__name__)

# -- Timeout & rate limit --
_TIMEOUT = 20  # seconds
_S2_RATE_LIMIT_SLEEP = 3  # Semantic Scholar without key: 1 req/sec is safe


# -- Unified data structure --

@dataclass
class DiscoveredPaper:
    title: str
    authors: list[str]
    year: int | None
    doi: str | None
    abstract: str
    citation_count: int | None
    open_access_pdf: str | None
    source: str  # "openalex" | "semantic_scholar" | "arxiv" | "crossref"
    url: str
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def is_in_library(self) -> bool:
        """Check if this paper is already in ChromaDB (metadata exact match, not semantic search)."""
        # 1) DOI exact match (ChromaDB metadata has doi field)
        if self.doi:
            if vector_store.exists_by_metadata("doi", self.doi):
                return True
        # 2) Title exact match (metadata has title field)
        if self.title:
            if vector_store.exists_by_metadata("title", self.title):
                return True
        return False


# -- HTTP Client (shared connection pool) --

_client: httpx.Client | None = None

def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=_TIMEOUT,
            headers={
                "User-Agent": f"ZoteroBrain/1.0 (mailto:{config.UNPAYWALL_EMAIL})",
            },
        )
    return _client


def close_client():
    """Explicitly close the shared HTTP client and release connections."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.debug("Discovery HTTP client closed")


# ============================================================================
# OpenAlex (primary, 240M+ papers, free, no key, polite pool, OA URL included)
# ============================================================================

_OPENALEX_BASE = "https://api.openalex.org/works"


def _reconstruct_abstract(inverted_index: dict) -> str:
    """
    Reconstruct plain-text abstract from OpenAlex abstract_inverted_index.
    Format: {"word": [pos1, pos2, ...], ...} -> sorted by position and joined.
    """
    if not inverted_index:
        return ""
    word_positions = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in word_positions)


def _search_openalex(query: str, limit: int = 10) -> list[DiscoveredPaper]:
    """OpenAlex search (primary source, 240M+ papers, free, polite pool acceleration)."""
    client = _get_client()
    try:
        resp = client.get(_OPENALEX_BASE, params={
            "search": query,
            "per_page": str(limit),
            "mailto": config.OPENALEX_EMAIL,
        })
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"OpenAlex API error: {e}")
        return []

    papers = []
    for w in data.get("results", []):
        # Title
        title = w.get("title", "")

        # Authors from authorships
        authors = []
        for authorship in w.get("authorships", []):
            author = authorship.get("author", {})
            name = author.get("display_name", "")
            if name:
                authors.append(name)

        # Year
        year = w.get("publication_year")

        # DOI (OpenAlex returns full URL like https://doi.org/10.xxx)
        doi = w.get("doi")
        if doi and doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]

        # Abstract from inverted index
        inverted_index = w.get("abstract_inverted_index") or {}
        abstract = _reconstruct_abstract(inverted_index)

        # Citation count
        cited_by = w.get("cited_by_count")

        # Open access PDF URL
        oa = w.get("open_access") or {}
        oa_url = oa.get("oa_url")

        # Work URL
        url = w.get("id", "")

        # Journal metadata from primary_location
        journal = ""
        volume = ""
        issue = ""
        pages = ""
        biblio = w.get("biblio") or {}
        volume = biblio.get("volume", "") or ""
        issue = biblio.get("issue", "") or ""
        first_page = biblio.get("first_page", "") or ""
        last_page = biblio.get("last_page", "") or ""
        if first_page and last_page:
            pages = f"{first_page}-{last_page}"
        elif first_page:
            pages = first_page
        primary_loc = w.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        journal = source.get("display_name", "") or ""

        papers.append(DiscoveredPaper(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            abstract=abstract,
            citation_count=cited_by,
            open_access_pdf=oa_url,
            source="openalex",
            url=url,
            journal=journal,
            volume=volume,
            issue=issue,
            pages=pages,
        ))

    return papers


# ============================================================================
# Semantic Scholar
# ============================================================================

_S2_BASE = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_FIELDS = "title,year,citationCount,externalIds,openAccessPdf,authors,abstract,url,journal"

def _search_semantic_scholar(query: str, limit: int = 10) -> list[DiscoveredPaper]:
    """Semantic Scholar search."""
    client = _get_client()
    try:
        resp = client.get(_S2_BASE, params={
            "query": query,
            "limit": str(limit),
            "fields": _S2_FIELDS,
        })
        if resp.status_code == 429:
            logger.warning("Semantic Scholar rate limited, sleeping 10s...")
            time.sleep(10)
            resp = client.get(_S2_BASE, params={
                "query": query,
                "limit": str(limit),
                "fields": _S2_FIELDS,
            })
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"Semantic Scholar API error: {e}")
        return []

    papers = []
    for p in data.get("data", []):
        ext_ids = p.get("externalIds") or {}
        doi = ext_ids.get("DOI")
        oa = p.get("openAccessPdf") or {}
        oa_url = oa.get("url")
        authors = [a["name"] for a in (p.get("authors") or [])]

        journal_info = p.get("journal") or {}
        journal = journal_info.get("name", "") or ""
        volume = journal_info.get("volume", "") or ""
        s2_pages = journal_info.get("pages", "") or ""

        papers.append(DiscoveredPaper(
            title=p.get("title", ""),
            authors=authors,
            year=p.get("year"),
            doi=doi,
            abstract=p.get("abstract", ""),
            citation_count=p.get("citationCount"),
            open_access_pdf=oa_url,
            source="semantic_scholar",
            url=p.get("url", ""),
            journal=journal,
            volume=volume,
            pages=s2_pages,
        ))

    return papers


# ============================================================================
# arXiv
# ============================================================================

_ARXIV_BASE = "https://export.arxiv.org/api/query"
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

def _search_arxiv(query: str, limit: int = 10) -> list[DiscoveredPaper]:
    """arXiv search."""
    client = _get_client()
    try:
        resp = client.get(_ARXIV_BASE, params={
            "search_query": f"all:{query}",
            "max_results": str(limit),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"arXiv API error: {e}")
        return []

    papers = []
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.error(f"arXiv XML parse error: {e}")
        return []

    for entry in root.findall("atom:entry", _ARXIV_NS):
        title_el = entry.find("atom:title", _ARXIV_NS)
        title = " ".join(title_el.text.split()) if title_el is not None and title_el.text else ""

        authors = []
        for author_el in entry.findall("atom:author", _ARXIV_NS):
            name_el = author_el.find("atom:name", _ARXIV_NS)
            if name_el is not None and name_el.text:
                authors.append(name_el.text)

        published_el = entry.find("atom:published", _ARXIV_NS)
        year = None
        if published_el is not None and published_el.text:
            try:
                year = int(published_el.text[:4])
            except ValueError:
                pass

        summary_el = entry.find("atom:summary", _ARXIV_NS)
        abstract = " ".join(summary_el.text.split()) if summary_el is not None and summary_el.text else ""

        # Extract DOI from arxiv:doi or id
        doi = None
        doi_el = entry.find("arxiv:doi", _ARXIV_NS)
        if doi_el is not None and doi_el.text:
            doi = doi_el.text
        else:
            id_el = entry.find("atom:id", _ARXIV_NS)
            if id_el is not None and id_el.text:
                arxiv_id_match = re.search(r"abs/([\d.]+)", id_el.text)
                if arxiv_id_match:
                    doi = None  # arXiv papers may not have DOI

        # PDF link
        pdf_url = None
        for link_el in entry.findall("atom:link", _ARXIV_NS):
            if link_el.get("title") == "pdf":
                pdf_url = link_el.get("href")
                break

        # arXiv URL
        url = ""
        id_el = entry.find("atom:id", _ARXIV_NS)
        if id_el is not None and id_el.text:
            url = id_el.text

        papers.append(DiscoveredPaper(
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            abstract=abstract,
            citation_count=None,  # arXiv doesn't provide this
            open_access_pdf=pdf_url,
            source="arxiv",
            url=url,
        ))

    return papers


# ============================================================================
# CrossRef
# ============================================================================

_CROSSREF_BASE = "https://api.crossref.org/works"

def _search_crossref(query: str, limit: int = 10) -> list[DiscoveredPaper]:
    """CrossRef search."""
    client = _get_client()
    try:
        resp = client.get(_CROSSREF_BASE, params={
            "query": query,
            "rows": str(limit),
            "select": "title,DOI,published,abstract,author,URL,link,is-referenced-by-count,container-title,volume,issue,page",
        }, headers={
            "User-Agent": f"ZoteroBrain/1.0 (mailto:{config.UNPAYWALL_EMAIL})",
        })
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"CrossRef API error: {e}")
        return []

    papers = []
    for item in data.get("message", {}).get("items", []):
        title_list = item.get("title", [])
        title = title_list[0] if title_list else ""

        authors = []
        for a in item.get("author", []):
            family = a.get("family", "")
            given = a.get("given", "")
            name = f"{family} {given}".strip()
            if name:
                authors.append(name)

        year = None
        pub = item.get("published", {})
        date_parts = pub.get("date-parts", [[None]])
        if date_parts and date_parts[0]:
            year = date_parts[0][0]

        # Try to get PDF link from CrossRef
        pdf_url = None
        for link in item.get("link", []):
            if link.get("content-type") == "application/pdf":
                pdf_url = link.get("URL")
                break

        papers.append(DiscoveredPaper(
            title=title,
            authors=authors,
            year=year,
            doi=item.get("DOI"),
            abstract=item.get("abstract", ""),
            citation_count=item.get("is-referenced-by-count"),
            open_access_pdf=pdf_url,
            source="crossref",
            url=item.get("URL", ""),
            journal=(item.get("container-title") or [""])[0] or "",
            volume=item.get("volume", "") or "",
            issue=item.get("issue", "") or "",
            pages=item.get("page", "") or "",
        ))

    return papers


# ============================================================================
# Unpaywall - check open access status
# ============================================================================

def check_open_access(doi: str) -> dict | None:
    """
    Check open access status for a given DOI.

    Returns: {"is_oa": bool, "pdf_url": str|None, "oa_status": str} or None
    """
    if not doi:
        return None

    client = _get_client()
    try:
        resp = client.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": config.UNPAYWALL_EMAIL},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"Unpaywall check failed for {doi}: {e}")
        return None

    is_oa = data.get("is_oa", False)
    oa_status = data.get("oa_status", "unknown")
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url")

    return {
        "is_oa": is_oa,
        "pdf_url": pdf_url,
        "oa_status": oa_status,
    }


# ============================================================================
# Public API
# ============================================================================

def discover(
    query: str,
    sources: list[str] | None = None,
    limit: int = 10,
    check_duplicates: bool = True,
) -> list[dict]:
    """
    Search for papers, return candidate list.

    Args:
        query: search keywords
        sources: data source list, default all. Options: ["openalex", "arxiv", "crossref", "semantic_scholar"]
        limit: results per source
        check_duplicates: whether to check if papers are already in ChromaDB

    Returns:
        [
            {
                "title": "...",
                "authors": [...],
                "year": 2025,
                "doi": "10.xxx/...",
                "abstract": "...",
                "citation_count": 42,
                "open_access_pdf": "https://..." | None,
                "source": "openalex",
                "url": "...",
                "in_library": False,
            },
            ...
        ]
    """
    if sources is None:
        sources = ["openalex", "arxiv", "crossref", "semantic_scholar"]

    all_papers: list[DiscoveredPaper] = []

    source_map = {
        "openalex": _search_openalex,
        "semantic_scholar": _search_semantic_scholar,
        "arxiv": _search_arxiv,
        "crossref": _search_crossref,
    }

    for src in sources:
        fn = source_map.get(src)
        if fn is None:
            logger.warning(f"Unknown source: {src}")
            continue
        logger.info(f"Searching {src} for: {query[:50]}")
        papers = fn(query, limit=limit)
        logger.info(f"  Found {len(papers)} papers from {src}")
        all_papers.extend(papers)

    # Deduplicate (by DOI or title)
    seen_dois = set()
    seen_titles = set()
    unique = []
    for p in all_papers:
        doi_key = (p.doi or "").lower()
        title_key = p.title.lower().strip()[:80]
        if doi_key and doi_key in seen_dois:
            continue
        if title_key in seen_titles:
            continue
        if doi_key:
            seen_dois.add(doi_key)
        seen_titles.add(title_key)
        unique.append(p)

    # Sort by citation_count (None goes last)
    unique.sort(key=lambda p: p.citation_count if p.citation_count is not None else -1, reverse=True)

    # Check if already in library
    results = []
    for p in unique:
        d = p.to_dict()
        if check_duplicates:
            d["in_library"] = p.is_in_library()
        else:
            d["in_library"] = False
        results.append(d)

    return results


def enrich_oa(papers: list[dict]) -> list[dict]:
    """
    Enrich candidate paper list with Unpaywall open access info.
    Skips papers that already have open_access_pdf; queries the rest one by one.
    """
    for p in papers:
        if p.get("open_access_pdf"):
            continue  # already has PDF link
        doi = p.get("doi")
        if not doi:
            continue
        oa = check_open_access(doi)
        if oa and oa["is_oa"] and oa["pdf_url"]:
            p["open_access_pdf"] = oa["pdf_url"]
            p["oa_status"] = oa["oa_status"]
        time.sleep(0.2)  # avoid Unpaywall rate limiting
    return papers
