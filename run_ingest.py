# -*- coding: utf-8 -*-
r"""
Zotero Brain - 批量入库脚本（MinerU 批量并行解析）

流水线:
  Phase 1: 下载/复制 PDF + 检查缓存
  Phase 2: 批量提交 MinerU（并行解析）
  Phase 3: 收集结果 → 切块 → Embedding → ChromaDB

用法:
  cd F:\MyProjects\zotero-brain
  .venv\Scripts\python.exe run_ingest.py                    # 全量入库
  .venv\Scripts\python.exe run_ingest.py --incremental      # 只入库新增
  .venv\Scripts\python.exe run_ingest.py --limit 10         # 最多处理10篇
  .venv\Scripts\python.exe run_ingest.py --collection 钠电   # 只处理"钠电"相关
  .venv\Scripts\python.exe run_ingest.py --batch-size 10    # 每批提交10篇到MinerU
"""
import sys
import io
import os
import logging
import json
import time
from pathlib import Path

# Ensure UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Ensure working directory is correct
os.chdir(Path(__file__).parent)

# Ensure data directories exist
Path("parsed").mkdir(exist_ok=True)
Path("data/chroma_db").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

import httpx

import config
import zotero_sync
import pdf_parser
import chunker
import vector_store

# Number of files per MinerU batch submission
MINERU_BATCH_SIZE = 20
# Polling timeout per batch (seconds)
MINERU_POLL_TIMEOUT = 1800


def _get_already_ingested() -> set[str]:
    """获取已入库的论文 key 集合"""
    keys = set()
    try:
        for col in vector_store.list_collections():
            keys |= vector_store.get_paper_keys(col["name"])
    except Exception:
        pass
    return keys


def _has_cached_parse(item_key: str, pdf_path: Path) -> bool:
    """检查是否有缓存的解析结果（key 命名优先，向后兼容 stem 命名）"""
    if (config.PARSED_DIR / item_key / f"{item_key}.md").exists():
        return True
    if (config.PARSED_DIR / item_key / f"{pdf_path.stem}.md").exists():
        return True
    return False


def _ensure_collection_mapping(col_name: str) -> None:
    """确保 Collection 有中英文映射，没有则自动生成 slug 并注册"""
    if col_name == config.DEFAULT_COLLECTION:
        return
    if col_name in config._NAME_MAP:
        return  # already mapped
    # Auto-generate slug: use pinyin-style hash for Chinese names
    import hashlib
    h = hashlib.md5(col_name.encode()).hexdigest()[:10]
    slug = f"col-{h}"
    config.register_collection_mapping(col_name, slug)
    logger.info(f"  自动注册映射: '{col_name}' → '{slug}'")


def _process_paper(item: dict, markdown_text: str) -> int:
    """切块 + Embedding + 入库，返回 chunk 数量"""
    key = item["key"]
    title = item.get("title", "?")
    paper_metadata = {
        "key": key,
        "title": title,
        "authors": ", ".join(item.get("authors", [])),
        "year": str(item.get("year", "")),
        "doi": item.get("doi", ""),
        "url": item.get("url", ""),
        "abstract": item.get("abstract", ""),
    }
    chunks = chunker.chunk_markdown(markdown_text, paper_metadata=paper_metadata)
    if not chunks:
        logger.warning(f"  {key}: no chunks")
        return 0

    target_collections = item.get("collection_names", [config.DEFAULT_COLLECTION])
    # Ensure all target collections have mappings
    for col_name in target_collections:
        _ensure_collection_mapping(col_name)

    total = 0
    for col_name in target_collections:
        n = vector_store.add_chunks(chunks, collection_name=col_name)
        total += n
        logger.info(f"  {key}: {n} chunks -> [{col_name}]")
    return total


def _submit_and_wait_batch(
    client: httpx.Client,
    pdf_paths: list[Path],
    item_keys: list[str],
) -> dict[str, str | None]:
    """
    Batch-submit PDFs to MinerU via raw httpx REST API.
    Returns {item_key: markdown_text | None}.
    """
    opts = {
        "model": config.MINERU_MODEL,
        "formula": True,
        "table": True,
        "language": "ch",
    }

    logger.info(f"  Submitting {len(pdf_paths)} PDFs to MinerU...")
    path_strs = [str(p) for p in pdf_paths]
    batch_id = pdf_parser._get_upload_urls(client, path_strs, opts, ocr=True, pages=None)
    logger.info(f"  Batch ID: {batch_id}")

    # Poll until all done/failed
    deadline = time.monotonic() + MINERU_POLL_TIMEOUT
    interval = 5.0
    results_map: dict[str, str | None] = {}

    while True:
        resp = client.get(f"{pdf_parser._API_BASE}/extract-results/batch/{batch_id}")
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"MinerU polling error: {body.get('message', 'unknown')}")

        results = body["data"]["extract_result"]
        done_count = sum(1 for r in results if r.get("state") in ("done", "failed"))
        total = len(results)
        logger.info(f"  Polling: {done_count}/{total} done")

        if done_count == total:
            # Download markdown + images for each result
            outputs = []
            for r in results:
                if r.get("state") != "done":
                    outputs.append(("", []))
                    continue
                try:
                    out = pdf_parser._download_results(client, [r])
                    outputs.append(out[0] if out else ("", []))
                except Exception as e:
                    logger.warning(f"  Download failed: {e}")
                    outputs.append(("", []))

            for i, (md_text, images) in enumerate(outputs):
                item_key = item_keys[i]
                if md_text:
                    cache_dir = config.PARSED_DIR / item_key
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    pdf_stem = pdf_paths[i].stem
                    cache_md = cache_dir / f"{pdf_stem}.md"
                    cache_md.write_text(md_text, encoding="utf-8")

                    if images:
                        images_dir = cache_dir / "images"
                        images_dir.mkdir(parents=True, exist_ok=True)
                        for img_name, img_data in images:
                            (images_dir / img_name).write_bytes(img_data)

                    results_map[item_key] = md_text
                    logger.info(f"  [{item_key}] done: {len(md_text)} chars")
                else:
                    logger.warning(f"  [{item_key}] failed or empty")
                    results_map[item_key] = None
            break

        if time.monotonic() > deadline:
            logger.error(f"  MinerU batch timeout ({MINERU_POLL_TIMEOUT}s)")
            for key in item_keys:
                if key not in results_map:
                    results_map[key] = None
            break

        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        interval = min(interval * 1.5, 30.0)

    return results_map


def run(
    incremental: bool = True,
    limit: int = 0,
    force_parse: bool = False,
    collection_filter: str | None = None,
    mineru_batch_size: int = MINERU_BATCH_SIZE,
):
    """运行完整入库管线"""
    print("=" * 60)
    print("  Zotero Brain - 批量入库管线")
    print("=" * 60)
    print(f"  本地 Zotero:     {config.ZOTERO_LOCAL_STORAGE}")
    print(f"  ChromaDB:        {config.CHROMA_DIR}")
    print(f"  Parsed:          {config.PARSED_DIR}")
    print(f"  Embedding:       {config.ZHIPU_MODEL}")
    print(f"  MinerU batch:    {mineru_batch_size}")
    print()

    # -- Phase 0: Fetch paper list --
    zot = zotero_sync._get_client()
    items = zotero_sync.list_items(zot, check_pdf=False)
    print(f"  Zotero 论文总数: {len(items)}")

    if collection_filter:
        items = [
            it for it in items
            if any(
                collection_filter.lower() in name.lower()
                for name in it.get("collection_names", [])
            )
        ]
        print(f"  过滤 '{collection_filter}': {len(items)} 篇")

    if incremental:
        already = _get_already_ingested()
        before = len(items)
        items = [it for it in items if it["key"] not in already]
        print(f"  增量模式: {before - len(items)} 已入库, {len(items)} 待处理")

    if limit > 0:
        items = items[:limit]
        print(f"  限制: 最多处理 {limit} 篇")

    if not items:
        print("\n  没有需要处理的论文")
        return

    print()

    # -- Phase 1: Download PDF + Check cache --
    print("=" * 60)
    print("  Phase 1: 下载 PDF + 检查缓存")
    print("=" * 60)

    cached: dict[str, tuple] = {}       # item_key -> (item, markdown_text)
    need_parse: list[tuple] = []        # [(item, pdf_path), ...]

    for i, item in enumerate(items, 1):
        key = item["key"]
        title = item.get("title", "?")[:50]

        # Download PDF (prefer local copy)
        pdf_path = zotero_sync.download_pdf(item_key=key)
        if pdf_path is None:
            logger.warning(f"  [{i}/{len(items)}] {key}: no PDF - SKIP")
            continue

        # Check cache
        if not force_parse and _has_cached_parse(key, pdf_path):
            cache_md = config.PARSED_DIR / key / f"{pdf_path.stem}.md"
            md = cache_md.read_text(encoding="utf-8")
            if md.strip():
                cached[key] = (item, md)
                logger.info(f"  [{i}/{len(items)}] {key}: cached ({len(md)} chars)")
                continue

        need_parse.append((item, pdf_path))
        logger.info(f"  [{i}/{len(items)}] {key}: {title} -> need parse ({pdf_path.stat().st_size / 1024 / 1024:.1f} MB)")

    print(f"\n  缓存命中: {len(cached)} 篇")
    print(f"  需要解析: {len(need_parse)} 篇")

    # -- Phase 2: Batch MinerU parsing --
    parsed: dict[str, tuple] = {}  # item_key -> (item, markdown_text)

    if need_parse:
        print()
        print("=" * 60)
        print("  Phase 2: MinerU 批量解析")
        print("=" * 60)

        http_client = httpx.Client(
            headers={"Authorization": f"Bearer {config.MINERU_TOKEN}"},
            timeout=30.0,
        )
        try:
            for batch_start in range(0, len(need_parse), mineru_batch_size):
                batch_end = min(batch_start + mineru_batch_size, len(need_parse))
                batch = need_parse[batch_start:batch_end]
                batch_num = batch_start // mineru_batch_size + 1
                total_batches = (len(need_parse) + mineru_batch_size - 1) // mineru_batch_size

                print(f"\n  --- Batch {batch_num}/{total_batches} ({len(batch)} papers) ---")

                pdf_paths = [pdf_path for _, pdf_path in batch]
                item_keys = [item["key"] for item, _ in batch]

                try:
                    results = _submit_and_wait_batch(http_client, pdf_paths, item_keys)
                    for item, pdf_path in batch:
                        key = item["key"]
                        md = results.get(key)
                        if md and md.strip():
                            parsed[key] = (item, md)
                except Exception as e:
                    logger.error(f"  Batch {batch_num} failed: {e}", exc_info=True)
                    # Fallback to per-paper parsing via pdf_parser (also httpx-based)
                    for item, pdf_path in batch:
                        key = item["key"]
                        try:
                            md = pdf_parser.parse_pdf(pdf_path, item_key=key, force=True)
                            if md and md.strip():
                                parsed[key] = (item, md)
                        except Exception as e2:
                            logger.error(f"  {key}: single-paper fallback also failed: {e2}")

                # Brief rest between batches to avoid API rate limiting
                if batch_end < len(need_parse):
                    time.sleep(2)
        finally:
            http_client.close()

    print(f"\n  MinerU 解析完成: {len(parsed)} 篇")

    # -- Phase 3: Chunking + Embedding + Ingestion --
    print()
    print("=" * 60)
    print("  Phase 3: 切块 + Embedding + 入库")
    print("=" * 60)

    # Merge cached + newly parsed results
    all_papers = {**cached, **parsed}

    stats = {
        "total": len(items),
        "cached": len(cached),
        "parsed": len(parsed),
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "chunks": 0,
    }

    for i, (key, (item, markdown_text)) in enumerate(all_papers.items(), 1):
        title = item.get("title", "?")[:50]
        cols = ", ".join(item.get("collection_names", []))
        print(f"\n[{i}/{len(all_papers)}] {title}")
        print(f"  Collection: {cols}")

        try:
            n = _process_paper(item, markdown_text)
            if n > 0:
                stats["success"] += 1
                stats["chunks"] += n
            else:
                stats["skipped"] += 1
        except Exception as e:
            logger.error(f"    FAILED: {e}", exc_info=True)
            stats["failed"] += 1

        # Rest every 5 papers to avoid Embedding API rate limiting
        if i % 5 == 0:
            time.sleep(0.5)

    # -- Statistics --
    print("\n" + "=" * 60)
    print("  入库统计")
    print(f"  总计:       {stats['total']} 篇")
    print(f"  缓存命中:   {stats['cached']} 篇")
    print(f"  MinerU解析: {stats['parsed']} 篇")
    print(f"  入库成功:   {stats['success']} 篇")
    print(f"  跳过:       {stats['skipped']} 篇")
    print(f"  失败:       {stats['failed']} 篇")
    print(f"  新增块:     {stats['chunks']} 个")
    print("=" * 60)

    # ChromaDB status
    print("\nChromaDB Collections:")
    try:
        for col in vector_store.list_collections():
            print(f"  {col['name']} (safe={col['safe_name']}): {col['count']} chunks")
    except Exception as e:
        print(f"  (error: {e})")

    # Save statistics
    stats_path = config.DATA_DIR / "last_ingest_stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"\nStats saved: {stats_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Zotero Brain Batch Ingest")
    parser.add_argument("--no-incremental", action="store_true", help="full ingest (ignore existing)")
    parser.add_argument("--incremental", action="store_true", help="only new papers (default)")
    parser.add_argument("--limit", type=int, default=0, help="max papers to process (0=all)")
    parser.add_argument("--force-parse", action="store_true", help="re-parse all PDFs")
    parser.add_argument("--collection", type=str, default=None, help="filter by collection name (fuzzy)")
    parser.add_argument("--batch-size", type=int, default=MINERU_BATCH_SIZE, help=f"MinerU batch size (default: {MINERU_BATCH_SIZE})")
    args = parser.parse_args()

    run(
        incremental=not args.no_incremental,
        limit=args.limit,
        force_parse=args.force_parse,
        collection_filter=args.collection,
        mineru_batch_size=args.batch_size,
    )
