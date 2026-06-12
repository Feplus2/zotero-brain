# -*- coding: utf-8 -*-
"""
PDF Parser - Use MinerU Cloud API (via httpx) to parse PDF into structured Markdown

Features:
  - Call MinerU VLM model via REST API (no local GPU, no heavy SDK)
  - Output structured Markdown (with tables, formulas, image descriptions)
  - Cache parsed results to parsed/ directory
  - Auto-retry on network errors
"""

import logging
import time
from pathlib import Path

import httpx

import config

logger = logging.getLogger(__name__)

# Max pages per slice (free API limit: 200 pages/request)
CHUNK_SIZE = 200
MAX_RETRIES = 3

# MinerU API endpoints
_API_BASE = "https://mineru.net/api/v4"
_POLL_INTERVAL_START = 2.0
_POLL_INTERVAL_MAX = 30.0


def _count_pages(pdf_path: str) -> int:
    """Get total page count of PDF"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        n = doc.page_count
        doc.close()
        return n
    except ImportError:
        # Fall back to pypdf if PyMuPDF is not available
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        return len(reader.pages)


def _get_upload_urls(client: httpx.Client, file_paths: list[str], opts: dict, ocr: bool, pages: str | None) -> str:
    """
    Request upload URLs for local files and submit extraction task.
    Returns batch_id.
    """
    files_meta = []
    for p in file_paths:
        entry = {
            "name": Path(p).name,
            "is_ocr": ocr,
        }
        if pages:
            entry["data_id"] = pages
        files_meta.append(entry)

    payload = {"files": files_meta, **opts}
    resp = client.post(f"{_API_BASE}/file-urls/batch", json=payload)
    resp.raise_for_status()
    body = resp.json()

    # Support both old format ({success: bool}) and new format ({code: 0})
    if body.get("code", body.get("success", True)) not in (0, True):
        raise RuntimeError(f"MinerU API error: {body.get('msg', body.get('message', 'unknown'))}")

    batch_id = body["data"]["batch_id"]

    # New API: file_urls is a flat list of presigned URLs
    # Old API: files is a list of dicts with upload_url
    file_urls = body["data"].get("file_urls")
    if file_urls is None:
        file_urls = [f["upload_url"] for f in body["data"]["files"]]

    for upload_url, local_path in zip(file_urls, file_paths):
        with open(local_path, "rb") as f:
            data = f.read()
        put_resp = client.put(upload_url, content=data)
        put_resp.raise_for_status()

    return batch_id


def _wait_for_batch(client: httpx.Client, batch_id: str, timeout: int = 600) -> list[dict]:
    """
    Poll batch status until all files are done or failed.
    Returns list of result dicts.
    """
    deadline = time.monotonic() + timeout
    interval = _POLL_INTERVAL_START

    while True:
        resp = client.get(f"{_API_BASE}/extract-results/batch/{batch_id}")
        resp.raise_for_status()
        body = resp.json()

        if body.get("code", body.get("success", True)) not in (0, True):
            raise RuntimeError(f"MinerU polling error: {body.get('msg', body.get('message', 'unknown'))}")

        results = body["data"]["extract_result"]
        if all(r.get("state") in ("done", "failed") for r in results):
            return results

        if time.monotonic() > deadline:
            raise TimeoutError(f"MinerU batch {batch_id} timed out after {timeout}s")

        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        interval = min(interval * 2, _POLL_INTERVAL_MAX)


def _download_results(client: httpx.Client, results: list[dict]) -> list[tuple[str, list[tuple[str, bytes]]]]:
    """
    Download markdown and images for completed results.
    Returns list of (markdown_text, [(image_name, image_bytes), ...]).
    """
    import zipfile
    import io

    outputs = []
    for r in results:
        if r.get("state") != "done":
            raise RuntimeError(f"MinerU extraction failed: {r.get('state')}")

        md_text = ""
        images = []

        # New API: full_zip_url contains everything (md + images)
        zip_url = r.get("full_zip_url") or r.get("markdown_url")
        if zip_url:
            zip_resp = client.get(zip_url)
            zip_resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
                for name in zf.namelist():
                    if name.endswith(".md") and not md_text:
                        md_text = zf.read(name).decode("utf-8")
                    elif name.rsplit(".", 1)[-1].lower() in ("png", "jpg", "jpeg", "gif", "svg"):
                        images.append((Path(name).name, zf.read(name)))

        # Old API fallback: separate markdown_url + content_list with img_path
        if not zip_url and r.get("content_list"):
            for img_info in r["content_list"]:
                if img_info.get("type") == "image" and img_info.get("img_path"):
                    img_url = img_info["img_path"]
                    img_resp = client.get(img_url)
                    img_resp.raise_for_status()
                    images.append((Path(img_url).name, img_resp.content))

        outputs.append((md_text, images))
    return outputs


def parse_pdf(
    pdf_path: Path,
    item_key: str = "",
    force: bool = False,
) -> str:
    """
    Parse a single PDF paper using MinerU Cloud API.

    Args:
        pdf_path: PDF file path
        item_key: Zotero paper key (for cache directory)
        force: Force re-parse (ignore cache)

    Returns: Structured Markdown text
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Cache directory: always use item_key, cache filename = {key}.md
    # This way ingest from different PDF paths (downloads/papers/storage) all share the same cache
    cache_dir = config.PARSED_DIR / (item_key or pdf_path.stem)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_md = cache_dir / f"{item_key or pdf_path.stem}.md"

    # Check cache
    if cache_md.exists() and not force:
        logger.info(f"Using cache: {cache_md}")
        return cache_md.read_text(encoding="utf-8")

    logger.info(f"Parsing: {pdf_path.name}")

    # Get page count, determine if chunking is needed
    total_pages = _count_pages(str(pdf_path))
    chunks_needed = (total_pages + CHUNK_SIZE - 1) // CHUNK_SIZE
    logger.info(f"  {total_pages} pages, {chunks_needed} chunk(s)")

    # Build options
    opts = {
        "model": config.MINERU_MODEL,
        "formula": True,
        "table": True,
        "language": "ch",
    }

    with httpx.Client(
        headers={"Authorization": f"Bearer {config.MINERU_TOKEN}"},
        timeout=30.0,
    ) as client:
        # network_helper.install() is already globally installed at mcp_server startup,
        # all httpx requests auto-routed to direct connect for mineru.net, no extra handling needed.

        all_markdown = []

        for chunk_idx in range(chunks_needed):
            start_page = chunk_idx * CHUNK_SIZE + 1
            end_page = min(start_page + CHUNK_SIZE - 1, total_pages)
            page_range = f"{start_page}-{end_page}"

            if chunks_needed > 1:
                logger.info(f"  Chunk {chunk_idx + 1}/{chunks_needed}: pages {start_page}-{end_page}")

            # Retry logic
            last_error = None
            for attempt in range(MAX_RETRIES):
                try:
                    # Upload and submit
                    batch_id = _get_upload_urls(client, [str(pdf_path)], opts, ocr=True, pages=page_range)

                    # Wait for completion
                    results = _wait_for_batch(client, batch_id, timeout=600)

                    # Download results
                    outputs = _download_results(client, results)

                    if not outputs:
                        raise RuntimeError("No results returned")

                    md_text, images = outputs[0]
                    all_markdown.append(md_text)

                    # Save images
                    if images:
                        images_dir = cache_dir / "images"
                        images_dir.mkdir(parents=True, exist_ok=True)
                        for img_name, img_data in images:
                            img_path = images_dir / img_name
                            img_path.write_bytes(img_data)

                    logger.info(f"    Done: {len(md_text):,} chars")
                    break

                except Exception as e:
                    last_error = e
                    if attempt < MAX_RETRIES - 1:
                        wait = (attempt + 1) * 10
                        logger.warning(f"    Retry {attempt + 1}/{MAX_RETRIES}: {e}, waiting {wait}s...")
                        time.sleep(wait)
            else:
                raise RuntimeError(f"MinerU parsing failed (retried {MAX_RETRIES} times): {last_error}")

        # Merge
        merged_md = "\n\n".join(all_markdown)

        # Save cache
        cache_md.write_text(merged_md, encoding="utf-8")

        logger.info(f"Parse complete: {pdf_path.name} -> {len(merged_md):,} chars")
        return merged_md


def parse_from_zotero_pdf(
    item_key: str,
    force: bool = False,
) -> str | None:
    """
    Parse from Zotero-downloaded PDF.

    Args:
        item_key: Zotero paper key
        force: Force re-parse

    Returns: Markdown text, or None (no PDF)
    """
    pdf_path = config.PARSED_DIR / item_key / f"{item_key}.pdf"
    if not pdf_path.exists():
        logger.warning(f"PDF not found: {pdf_path}")
        return None

    return parse_pdf(pdf_path, item_key, force)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python pdf_parser.py <pdf_path> [item_key]")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    item_key = sys.argv[2] if len(sys.argv) > 2 else pdf_path.stem

    result = parse_pdf(pdf_path, item_key)
    print(f"\n{'='*60}")
    print(f"Parse complete, {len(result)} chars total")
    print(f"First 500 chars:")
    print(result[:500])
