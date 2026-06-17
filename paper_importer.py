# -*- coding: utf-8 -*-
"""
Paper Importer - download PDF -> import to Zotero -> trigger ingest pipeline.

Download cascade (6 levels):
  1. Local cache (previously downloaded PDFs)
  2. OpenAlex OA URL (open access, direct link)
  3. Unpaywall (open access, legal)
  4. CORE API (open access, requires key)
  5. arXiv direct download (preprints)
  6. Sci-Hub mirror rotation (grey area, broad coverage)
  7. Fail -> prompt user for manual download

Import flow:
  Download PDF -> pyzotero create item (no upload, cloud stores metadata only)
  -> add to Collection -> trigger _ingest_paper(pdf_path=local path) -> MinerU -> chunk -> vectorize

Dependencies: httpx, pyzotero, scidownl (all installed)
"""

import logging
import time
from pathlib import Path

import httpx

import config
import zotero_sync
import paper_discovery

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # download timeout in seconds


# ============================================================================
# PDF Download Cascade
# ============================================================================

def _download_unpaywall(doi: str, save_dir: Path) -> Path | None:
    """Level 3: Unpaywall - legal open access."""
    if not doi:
        return None
    try:
        resp = httpx.get(
            f"https://api.unpaywall.org/v2/{doi}",
            params={"email": config.UNPAYWALL_EMAIL},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        best = data.get("best_oa_location") or {}
        pdf_url = best.get("url")
        if not pdf_url:
            return None
        return _fetch_pdf(pdf_url, save_dir, "unpaywall")
    except Exception as e:
        logger.warning(f"Unpaywall download failed: {e}")
        return None


def _download_arxiv(arxiv_url: str | None, save_dir: Path) -> Path | None:
    """Level 5: arXiv direct download from preprint server."""
    if not arxiv_url:
        return None
    # arxiv_url is usually http://arxiv.org/pdf/xxx.pdf or https://...
    pdf_url = arxiv_url.replace("http://", "https://")
    if not pdf_url.endswith(".pdf"):
        pdf_url += ".pdf"
    try:
        return _fetch_pdf(pdf_url, save_dir, "arxiv")
    except Exception as e:
        logger.warning(f"arXiv download failed: {e}")
        return None


def _download_openalex(oa_url: str | None, save_dir: Path) -> Path | None:
    """Level 2: OpenAlex OA URL - direct open access PDF download."""
    if not oa_url:
        return None
    try:
        return _fetch_pdf(oa_url, save_dir, "openalex")
    except Exception as e:
        logger.warning(f"OpenAlex OA download failed: {e}")
        return None


def _download_core(doi: str, save_dir: Path) -> Path | None:
    """Level 4: CORE API - search by DOI, extract downloadUrl."""
    if not doi or not config.CORE_API_KEY:
        return None
    try:
        resp = httpx.get(
            "https://api.core.ac.uk/v3/search/works",
            params={"q": f"doi:{doi}", "limit": "1"},
            headers={"Authorization": f"Bearer {config.CORE_API_KEY}"},
            timeout=15,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning(f"CORE API returned {resp.status_code}")
            return None
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        download_url = results[0].get("downloadUrl")
        if not download_url:
            return None
        return _fetch_pdf(download_url, save_dir, "core")
    except Exception as e:
        logger.warning(f"CORE download failed: {e}")
        return None


# -- Sci-Hub mirror rotation --

_SCIHUB_MIRROR_CACHE: list[str] = []
_SCIHUB_MIRROR_CACHE_TIME: float = 0
_SCIHUB_MIRROR_CACHE_TTL = 3600  # 1 hour


def _get_scihub_mirrors() -> list[str]:
    """Fetch active Sci-Hub mirrors from whereisscihub, cached for 1 hour."""
    global _SCIHUB_MIRROR_CACHE, _SCIHUB_MIRROR_CACHE_TIME
    import time as _time
    now = _time.time()
    if _SCIHUB_MIRROR_CACHE and (now - _SCIHUB_MIRROR_CACHE_TIME) < _SCIHUB_MIRROR_CACHE_TTL:
        return _SCIHUB_MIRROR_CACHE
    try:
        resp = httpx.get(
            "https://whereisscihub-rs28c.ondigitalocean.app/",
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        # Response format: one mirror URL per line
        text = resp.text.strip()
        mirrors = [line.strip().rstrip("/") for line in text.split("\n") if line.strip().startswith("http")]
        if mirrors:
            _SCIHUB_MIRROR_CACHE = mirrors
            _SCIHUB_MIRROR_CACHE_TIME = now
            logger.info(f"Sci-Hub mirrors refreshed: {len(mirrors)} mirrors")
            return mirrors
    except Exception as e:
        logger.warning(f"Failed to fetch Sci-Hub mirrors: {e}")
    # fallback: hardcoded common mirrors
    return [
        "https://sci-hub.se",
        "https://sci-hub.st",
        "https://sci-hub.ru",
    ]


def _download_scihub(doi: str, save_dir: Path) -> Path | None:
    """Level 6: Sci-Hub mirror rotation - try each active mirror in sequence."""
    if not doi:
        return None
    import re
    mirrors = _get_scihub_mirrors()
    for mirror in mirrors:
        try:
            url = f"{mirror}/{doi}"
            logger.info(f"Trying Sci-Hub mirror: {mirror}")
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                # Sci-Hub may return PDF directly or an HTML page
                if "pdf" in content_type or "octet-stream" in content_type:
                    return _save_pdf(resp.content, save_dir, "scihub")
                # HTML page: extract PDF URL using multiple patterns (new Sci-Hub layout)
                html = resp.text
                pdf_url = None

                # Pattern 1: <meta name="citation_pdf_url" content="/storage/...">
                m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', html)
                if m:
                    pdf_url = m.group(1)

                # Pattern 2: <object type="application/pdf" data="/storage/...">
                if not pdf_url:
                    m = re.search(r'<object[^>]+type=["\']application/pdf["\'][^>]+data=["\']([^"\']+)["\']', html)
                    if m:
                        pdf_url = m.group(1)

                # Pattern 3: iframe/embed src
                if not pdf_url:
                    m = re.search(r'(?:src|href)=["\']([^"\']*\.pdf[^"\']*)["\']', html)
                    if m:
                        pdf_url = m.group(1)

                if pdf_url:
                    if pdf_url.startswith("//"):
                        pdf_url = "https:" + pdf_url
                    elif pdf_url.startswith("/"):
                        pdf_url = mirror + pdf_url
                    logger.info(f"  Found PDF URL: {pdf_url[:80]}")
                    pdf = _fetch_pdf(pdf_url, save_dir, "scihub")
                    if pdf:
                        return pdf
                else:
                    logger.info(f"  No PDF URL found on {mirror}")
        except Exception as e:
            logger.warning(f"Sci-Hub mirror {mirror} failed: {e}")
            continue
    logger.warning("All Sci-Hub mirrors exhausted")
    return None


def _is_valid_pdf(content: bytes) -> bool:
    """PDF 文件必须以 %PDF 开头（ISO 32000 规范）"""
    return len(content) >= 4 and content[:4] == b'%PDF'


def _save_pdf(content: bytes, save_dir: Path, source: str) -> Path | None:
    """Save PDF content to file."""
    save_dir.mkdir(parents=True, exist_ok=True)
    if len(content) < 1000:
        logger.warning(f"PDF too small ({len(content)} bytes) from {source}")
        return None
    if not _is_valid_pdf(content):
        logger.warning(f"Invalid PDF (bad magic bytes, got {content[:200]!r}) from {source}")
        return None
    filename = f"download_{source}.pdf"
    pdf_path = save_dir / filename
    pdf_path.write_bytes(content)
    logger.info(f"Saved PDF from {source}: {pdf_path} ({len(content)} bytes)")
    return pdf_path


def _fetch_pdf(url: str, save_dir: Path, source: str) -> Path | None:
    """Generic PDF download via HTTP."""
    save_dir.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type and "octet-stream" not in content_type:
                logger.warning(f"URL returned non-PDF content-type: {content_type}")
                return None
            if not _is_valid_pdf(resp.content):
                logger.warning(f"Invalid PDF (bad magic bytes, content-type={content_type}) from {source}")
                return None
            filename = f"download_{source}.pdf"
            pdf_path = save_dir / filename
            pdf_path.write_bytes(resp.content)
            if pdf_path.stat().st_size < 1000:
                logger.warning(f"Downloaded PDF too small ({pdf_path.stat().st_size} bytes)")
                pdf_path.unlink(missing_ok=True)
                return None
            logger.info(f"Downloaded PDF from {source}: {pdf_path} ({pdf_path.stat().st_size} bytes)")
            return pdf_path
    except Exception as e:
        logger.warning(f"PDF fetch failed ({source}): {e}")
        return None


def _sanitize_filename_component(s: str) -> str:
    """Replace characters illegal in Windows/NTFS filenames."""
    for ch in r'<>:"/\|?*':
        s = s.replace(ch, '_')
    s = s.strip('. ')
    return s or "unnamed"


def _safe_filename(paper: dict) -> str:
    """Generate a unique filename for a paper (based on DOI or title hash) to avoid overwrites."""
    doi = paper.get("doi")
    if doi:
        safe = _sanitize_filename_component(doi)
        return f"doi_{safe}.pdf"
    title = paper.get("title", "unknown")[:60]
    import hashlib
    h = hashlib.md5(title.encode()).hexdigest()[:10]
    return f"title_{h}.pdf"


def _find_cached_pdf(paper: dict, save_dir: Path) -> Path | None:
    """Find a previously downloaded PDF in local cache (by DOI or filename match)."""
    safe = _safe_filename(paper)
    cached = save_dir / safe
    if cached.exists() and cached.stat().st_size > 1000:
        return cached
    # Fallback: check legacy download_*.pdf files (compat with old naming)
    for f in save_dir.glob("download_*.pdf"):
        if f.stat().st_size > 1000:
            # Only use if it's the only PDF in save_dir
            all_pdfs = list(save_dir.glob("download_*.pdf"))
            if len(all_pdfs) == 1:
                logger.info(f"Using legacy cached PDF: {f.name}")
                return f
    return None


def _rename_to_unique(pdf: Path, paper: dict, save_dir: Path) -> Path:
    """Rename downloaded PDF to a unique filename (based on DOI/title hash)."""
    final = save_dir / _safe_filename(paper)
    if pdf != final:
        pdf.rename(final)
    return final


def _verify_pdf_match(pdf_path: Path, expected_title: str) -> bool:
    """
    Verify that downloaded PDF metadata title matches expected paper title.

    Three-level fuzzy matching:
      1. Partial containment (one title contained in the other)
      2. SequenceMatcher ratio >= 0.6
      3. Word-level overlap >= 50%

    Returns False only when there is a clear mismatch.
    Returns True when titles match or when PDF metadata is absent (cannot verify).
    """
    if not expected_title.strip():
        return True

    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        meta = doc.metadata
        doc.close()
    except Exception as e:
        logger.warning(f"PDF metadata extraction failed: {e}, skipping verification")
        return True

    actual_title = (meta.get("title") or "").strip()
    if not actual_title:
        return True  # no embedded title, cannot verify — pass through

    a_lower = actual_title.lower()
    e_lower = expected_title.lower()

    # Level 1: partial containment
    if a_lower in e_lower or e_lower in a_lower:
        return True

    # Level 2: sequence similarity
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(None, a_lower, e_lower).ratio()
    if ratio >= 0.3:
        return True

    # Level 3: word-level overlap
    import re
    def _tokenize(s):
        return set(re.findall(r'[a-z0-9]{3,}', s))

    a_words = _tokenize(a_lower)
    e_words = _tokenize(e_lower)
    if a_words and e_words:
        overlap = len(a_words & e_words)
        min_words = min(len(a_words), len(e_words))
        if overlap / max(min_words, 1) >= 0.25:
            return True

    return False  # all three levels failed, likely mismatched paper


def download_pdf(
    paper: dict,
    save_dir: Path | None = None,
    verify: bool = True,
) -> tuple[Path | None, str]:
    """
    6-level cascade PDF download.

    Order:
      1. Local cache (previously downloaded PDFs)
      2. OpenAlex OA URL (open access direct link)
      3. Unpaywall (open access, legal)
      4. CORE API (open access, requires key)
      5. arXiv direct download (preprints)
      6. Sci-Hub mirror rotation (grey area)
      7. Fail -> return None

    Args:
        paper: paper dict from discover()
        save_dir: save directory

    Returns: (pdf_path, source)
        source: "cache" | "openalex" | "unpaywall" | "core" | "arxiv" | "scihub" | "none"
    """
    doi = paper.get("doi")
    oa_url = paper.get("open_access_pdf")
    expected_title = paper.get("title", "")

    if save_dir is None:
        save_dir = config.DATA_DIR / "downloads"
    save_dir.mkdir(parents=True, exist_ok=True)

    def _accept(pdf_path: Path, source: str) -> tuple[Path, str] | None:
        """Validate PDF match (if verify enabled) and return final path + source, or None to skip."""
        final = _rename_to_unique(pdf_path, paper, save_dir)
        if verify and expected_title:
            if not _verify_pdf_match(final, expected_title):
                logger.warning(
                    f"  PDF metadata mismatch for '{expected_title[:60]}...', discarding PDF from {source}"
                )
                final.unlink(missing_ok=True)
                return None
        return final, source

    # 1) Local cache
    cached = _find_cached_pdf(paper, save_dir)
    if cached:
        logger.info(f"Using cached PDF: {cached}")
        return cached, "cache"

    # 2) OpenAlex OA URL
    if oa_url:
        logger.info(f"Trying OpenAlex OA: {oa_url[:80]}")
        pdf = _download_openalex(oa_url, save_dir)
        if pdf:
            result = _accept(pdf, "openalex")
            if result:
                return result
        time.sleep(0.5)

    # 3) Unpaywall
    logger.info(f"Trying Unpaywall for DOI: {doi}")
    pdf = _download_unpaywall(doi, save_dir)
    if pdf:
        result = _accept(pdf, "unpaywall")
        if result:
            return result
    time.sleep(0.5)

    # 4) CORE API
    if config.CORE_API_KEY and doi:
        logger.info(f"Trying CORE for DOI: {doi}")
        pdf = _download_core(doi, save_dir)
        if pdf:
            result = _accept(pdf, "core")
            if result:
                return result
        time.sleep(0.5)

    # 5) arXiv (only for arxiv source or when OA URL is arxiv PDF)
    arxiv_url = oa_url if paper.get("source") == "arxiv" else None
    if arxiv_url:
        logger.info(f"Trying arXiv: {arxiv_url}")
        pdf = _download_arxiv(arxiv_url, save_dir)
        if pdf:
            result = _accept(pdf, "arxiv")
            if result:
                return result
        time.sleep(0.5)

    # 6) Sci-Hub mirror rotation
    if doi:
        logger.info(f"Trying Sci-Hub mirrors for DOI: {doi}")
        pdf = _download_scihub(doi, save_dir)
        if pdf:
            result = _accept(pdf, "scihub")
            if result:
                return result

    logger.warning(f"All download methods failed for: {paper.get('title', '?')[:60]}")
    return None, "none"


# ============================================================================
# Zotero Import
# ============================================================================

def _find_in_zotero(doi: str, title: str = "") -> str | None:
    """
    Find existing item in Zotero (by DOI match).

    Strategy:
      1. Title keyword search (fast, Zotero q= does NOT index DOI)
      2. Direct DOI search via q= (may match DOI in notes/URL)
      3. Full scan with safety cap via _fetch_all_items (retry-enabled, up to 1000 items)

    Returns: Zotero item key if found, else None
    """
    if not doi:
        return None
    zot = zotero_sync._get_client()
    try:
        doi_lower = doi.lower()
        skip_types = ("attachment", "note", "annotation")

        # 1. Title keyword search (fast, narrows candidates)
        search_terms = title.split()[:4] if title else []
        if search_terms:
            query = " ".join(search_terms)
            for item in zot.items(q=query):
                data = item["data"]
                if data.get("itemType") in skip_types:
                    continue
                if data.get("DOI", "").lower() == doi_lower:
                    return data["key"]

        # 2. Direct DOI search (may match via notes/URL/tags)
        for item in zot.items(q=doi):
            data = item["data"]
            if data.get("itemType") in skip_types:
                continue
            if data.get("DOI", "").lower() == doi_lower:
                return data["key"]

        # 3. Full scan with safety cap (uses retry-enabled _fetch_all_items)
        candidates = zotero_sync._fetch_all_items(zot, max_items=1000)
        for item in candidates:
            data = item["data"]
            if data.get("itemType") in skip_types:
                continue
            if data.get("DOI", "").lower() == doi_lower:
                return data["key"]
    except Exception as e:
        logger.warning(f"Zotero DOI lookup failed: {e}")
    return None


def _parse_authors(author_list: list[str]) -> list[dict]:
    """Convert 'Last First' or 'First Last' format to pyzotero creator format."""
    creators = []
    for name in author_list:
        parts = name.strip().split()
        if len(parts) >= 2:
            # Assume last token is surname (Last First format is more common in academic DBs)
            # Also handle First Last format
            if len(parts) == 2:
                # "Last First" or "First Last" - ambiguous, conservatively use last token as given name
                last = parts[0]
                first = " ".join(parts[1:])
            else:
                last = parts[-1]
                first = " ".join(parts[:-1])
            creators.append({
                "creatorType": "author",
                "firstName": first,
                "lastName": last,
            })
        elif parts:
            creators.append({
                "creatorType": "author",
                "firstName": "",
                "lastName": parts[0],
            })
    return creators


def import_to_zotero(
    paper: dict,
    pdf_path: Path | None = None,
    collection_name: str | None = None,
) -> str | None:
    """
    Import a paper into Zotero.

    Behavior:
      - Create journalArticle item (metadata stored in Zotero cloud)
      - If PDF available, create linked attachment (PDF stays local, not uploaded)
      - Item auto-added to specified Collection

    Args:
        paper: paper dict from discover()
        pdf_path: downloaded PDF path (optional, creates item only if None)
        collection_name: target Collection Chinese name (optional)

    Returns: Zotero item key, or None on failure
    """
    zot = zotero_sync._get_client()

    # Deduplication: check if paper already exists by DOI/title match
    doi = paper.get("doi", "")
    title = paper.get("title", "")
    existing_key = _find_in_zotero(doi, title)
    if existing_key:
        logger.info(f"Paper already in Zotero (key={existing_key}): {title[:60]}")
        if collection_name:
            try:
                collections = zotero_sync.list_collections(zot)
                col_key = None
                for c in collections:
                    if c["name"] == collection_name:
                        col_key = c["key"]
                        break
                if col_key is None:
                    col_key = zotero_sync.create_folder(collection_name, zot=zot)
                    config.ensure_collection_mapping(collection_name)
                zotero_sync.add_to_collection(existing_key, col_key, zot=zot)
                logger.info(f"Added existing item {existing_key} to collection {collection_name}")
            except Exception as e:
                logger.warning(f"Failed to add existing item to collection: {e}")
        return existing_key

    # Create item template
    template = zot.item_template("journalArticle")
    template["title"] = paper.get("title", "")
    template["DOI"] = paper.get("doi", "")
    template["date"] = str(paper.get("year", ""))
    template["abstractNote"] = paper.get("abstract", "")
    template["url"] = paper.get("url", "")
    if paper.get("journal"):
        template["publicationTitle"] = paper["journal"]
    if paper.get("volume"):
        template["volume"] = paper["volume"]
    if paper.get("issue"):
        template["issue"] = paper["issue"]
    if paper.get("pages"):
        template["pages"] = paper["pages"]

    # Authors
    creators = _parse_authors(paper.get("authors", []))
    if creators:
        template["creators"] = creators

    # Add to Collection - resolve or auto-create
    col_key = None
    used_template_binding = False
    if collection_name:
        try:
            collections = zotero_sync.list_collections(zot)
            for c in collections:
                if c["name"] == collection_name:
                    col_key = c["key"]
                    break
            if col_key:
                template["collections"] = [col_key]
                used_template_binding = True
            else:
                # Auto-create the missing collection
                logger.info(f"Collection not found, auto-creating: {collection_name}")
                col_key = zotero_sync.create_folder(collection_name, zot=zot)
                logger.info(f"Auto-created collection: {collection_name} (key={col_key})")
                config.ensure_collection_mapping(collection_name)
        except Exception as e:
            logger.warning(f"Failed to resolve or create collection '{collection_name}': {e}")

    try:
        response = zot.create_items([template])
        if response.get("failed"):
            logger.error(f"Zotero create failed: {response['failed']}")
            return None
        item_key = response["success"]["0"]
        col_info = f" in collection {collection_name}" if col_key else ""
        logger.info(f"Zotero item created: {item_key}{col_info}")
    except Exception as e:
        logger.error(f"Zotero create error: {e}")
        return None

    # Create linked attachment - archive PDF to permanent storage first
    if pdf_path and pdf_path.exists():
        try:
            # Archive: move PDF from temp downloads to permanent papers/ dir
            archive_path = _archive_pdf(pdf_path, paper)
            linked_att = {
                "itemType": "attachment",
                "linkMode": "linked_file",
                "path": str(archive_path.resolve()),
                "contentType": "application/pdf",
                "title": archive_path.name,
            }
            att_resp = zot.create_items([linked_att], parentid=item_key)
            if att_resp.get("failed"):
                logger.warning(f"Linked attachment failed: {att_resp['failed']}")
            else:
                logger.info(f"Linked attachment created: {archive_path.name} (archived)")
        except Exception as e:
            logger.warning(f"Failed to create linked attachment: {e}")

    # Fallback: add to collection after creation if not bound via template
    if col_key and not used_template_binding:
        try:
            zotero_sync.add_to_collection(item_key, col_key, zot=zot)
        except Exception as e:
            logger.error(f"Failed to add item {item_key} to collection {collection_name}: {e}")

    return item_key


def _archive_pdf(pdf_path: Path, paper: dict, allow_rename: bool = True) -> Path:
    """
    将 PDF 从临时下载目录归档到永久存储 (data/papers/)。

    使用 os.replace() 原子操作消除 TOCTOU 竞态窗口。
    两个线程同时归档同一篇论文时，首个成功写入者为准。

    Args:
        pdf_path: 源 PDF 路径
        paper: 论文元数据
        allow_rename: True=激进模式（import 用）— 原子 move 到规范名；
                      False=安全模式（ingest 用）— copy 到规范名，不删源文件
    """
    import shutil
    import os as _os

    archive_name = _safe_filename(paper)
    archive_path = config.PAPERS_DIR / archive_name

    # 规范名文件已存在，清理临时文件
    if archive_path.exists():
        if pdf_path != archive_path and pdf_path.exists():
            if allow_rename:
                pdf_path.unlink(missing_ok=True)
                logger.info(f"PDF already archived, cleaned temp: {pdf_path.name}")
        return archive_path

    # 已在 papers/ 中
    if pdf_path.parent == config.PAPERS_DIR:
        if allow_rename and pdf_path.name != archive_name:
            shutil.move(str(pdf_path), str(archive_path))
            logger.info(f"PDF renamed in papers/: {pdf_path.name} → {archive_name}")
            return archive_path
        return pdf_path

    # 来自外部目录 (downloads/ Zotero storage 等)
    if allow_rename:
        shutil.move(str(pdf_path), str(archive_path))
        logger.info(f"PDF archived: {pdf_path.name} → {archive_path}")
    else:
        tmp = archive_path.with_suffix(archive_path.suffix + '.tmp')
        shutil.copy2(str(pdf_path), str(tmp))
        _os.replace(str(tmp), str(archive_path))
        logger.info(f"PDF copied to papers/: {pdf_path.name} → {archive_path}")
    return archive_path


# ============================================================================
# Full pipeline: download -> import to Zotero -> ingest
# ============================================================================

def fetch_and_ingest(
    paper: dict,
    collection_name: str | None = None,
    force: bool = False,
) -> dict:
    """
    Complete paper fetch + ingest pipeline.

    Args:
        paper: paper dict from discover()
        collection_name: target Collection
        force: force re-processing even if already exists

    Returns: {
        "status": "success" | "partial" | "failed" | "skipped",
        "item_key": "...",
        "download_source": "openalex" | "unpaywall" | "arxiv" | "scihub" | "core" | "none",
        "chunks_added": int,
        "message": "...",
    }
    """
    title = paper.get("title", "?")
    doi = paper.get("doi")

    # Duplicate check 1: ChromaDB metadata match
    if not force and doi:
        try:
            existing = paper_discovery.DiscoveredPaper(**paper).is_in_library()
            if existing:
                return {
                    "status": "skipped",
                    "item_key": None,
                    "download_source": None,
                    "chunks_added": 0,
                    "message": f"Paper already in knowledge base: {title[:60]}",
                }
        except Exception as e:
            logger.warning(f"ChromaDB duplicate check failed: {e}")

    # Duplicate check 2: Zotero DOI exact match (catches cases where ChromaDB missed it)
    if not force and doi:
        existing_key = _find_in_zotero(doi, title)
        if existing_key:
            return {
                "status": "skipped",
                "item_key": existing_key,
                "download_source": None,
                "chunks_added": 0,
                "message": f"Paper already in Zotero (key={existing_key}): {title[:60]}",
            }

    # Step 1: Download PDF
    logger.info(f"=== Fetch & Ingest: {title[:60]} ===")
    save_dir = config.DATA_DIR / "downloads"
    pdf_path, dl_source = download_pdf(paper, save_dir)

    if pdf_path is None:
        # Still create Zotero item (without PDF)
        item_key = import_to_zotero(paper, None, collection_name)
        # Build manual download hints
        doi = paper.get("doi")
        manual_hints = []
        if doi:
            manual_hints.append(f"DOI: https://doi.org/{doi}")
            manual_hints.append(f"Sci-Hub: https://sci-hub.se/{doi}")
        if paper.get("url"):
            manual_hints.append(f"Paper page: {paper['url']}")
        hint_text = "\n".join(manual_hints) if manual_hints else "No DOI info"
        return {
            "status": "partial",
            "item_key": item_key,
            "download_source": "none",
            "chunks_added": 0,
            "message": (
                f"PDF download failed (6-level cascade exhausted), Zotero item created.\n"
                f"Paper: {title[:80]}\n\n"
                f"Manual download links:\n{hint_text}\n\n"
                f"After downloading PDF, run ingest_paper(key={item_key}) to ingest."
            ),
        }

    # Step 2: Import to Zotero
    item_key = import_to_zotero(paper, pdf_path, collection_name)
    if item_key is None:
        return {
            "status": "failed",
            "item_key": None,
            "download_source": dl_source,
            "chunks_added": 0,
            "message": f"Zotero import failed (PDF downloaded to {pdf_path})",
        }

    # Step 3: Trigger ingest pipeline (MinerU -> chunk -> vectorize)
    try:
        # Lazy import to avoid circular dependency:
        #   mcp_server.py imports paper_importer at module level
        #   paper_importer imports mcp_server only here (inside function)
        import mcp_server

        target_item = {
            "key": item_key,
            "title": title,
            "authors": paper.get("authors", []),
            "year": paper.get("year"),
            "doi": doi or "",
            "url": paper.get("url", ""),
            "abstract": paper.get("abstract", ""),
            "journal": paper.get("journal", ""),
            "volume": paper.get("volume", ""),
            "issue": paper.get("issue", ""),
            "pages": paper.get("pages", ""),
            "collection_names": [collection_name] if collection_name else [config.DEFAULT_COLLECTION],
        }

        ingest_result = mcp_server._ingest_paper(target_item, force_parse=force, pdf_path=str(pdf_path))
        chunks_added = ingest_result["added"]

        return {
            "status": "success",
            "item_key": item_key,
            "download_source": dl_source,
            "chunks_added": chunks_added,
            "message": f"Done! {title[:60]}\nSource: {dl_source} | Zotero key: {item_key} | {chunks_added} chunks",
        }

    except Exception as e:
        logger.error(f"Ingest failed: {e}")
        return {
            "status": "partial",
            "item_key": item_key,
            "download_source": dl_source,
            "chunks_added": 0,
            "message": (
                f"PDF download + Zotero import succeeded, but ingest failed: {e}\n"
                f"PDF: {pdf_path}\n"
                f"Run ingest_paper(key={item_key}) manually."
            ),
        }
