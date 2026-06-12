# -*- coding: utf-8 -*-
"""
增量补全入库 —— 将 parsed/ 中已解析但未入库的论文入 ChromaDB

跳过了 PDF 下载和 MinerU 解析（已有 MD 缓存），
只做 chunk → embed → vector_store
"""
import logging

import config
import chunker
import vector_store
import zotero_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("resume")


def main():
    # 1. Scan all parsed paper keys under parsed/
    parsed_dir = config.PARSED_DIR
    parsed_keys = set()
    for d in sorted(parsed_dir.iterdir()):
        if d.is_dir():
            md_file = d / f"{d.name}.md"
            if md_file.exists():
                parsed_keys.add(d.name)

    logger.info(f"parsed/ 中有 {len(parsed_keys)} 篇已解析论文")

    # 2. Fetch Zotero paper list (metadata needed)
    logger.info("连接 Zotero...")
    zot = zotero_sync._get_client()
    items = zotero_sync.list_items(zot=zot, check_pdf=False)
    item_map = {it["key"]: it for it in items}
    logger.info(f"Zotero 共 {len(item_map)} 篇论文")

    # 3. Check which keys are not yet in the database
    all_collections = vector_store.list_collections()
    known_keys = set()
    for col in all_collections:
        keys = vector_store.get_paper_keys(col["name"])
        known_keys.update(keys)
    logger.info(f"ChromaDB 中已入库 {len(known_keys)} 个论文 key")

    missing = parsed_keys - known_keys
    logger.info(f"待补全: {len(missing)} 篇")
    if not missing:
        logger.info("全部入库完毕，无需补全！")
        return

    # 4. Backfill one by one
    total_chunks = 0
    for i, key in enumerate(sorted(missing), 1):
        md_path = parsed_dir / key / f"{key}.md"
        if not md_path.exists():
            logger.warning(f"  [{i}/{len(missing)}] {key}: MD 文件不存在，跳过")
            continue

        item = item_map.get(key)
        if item is None:
            logger.warning(f"  [{i}/{len(missing)}] {key}: Zotero 中找不到，跳过")
            continue

        title = item.get("title", "?")[:60]
        logger.info(f"[{i}/{len(missing)}] {key}: {title}")

        markdown_text = md_path.read_text("utf-8")
        if not markdown_text.strip():
            logger.warning(f"  空 MD，跳过")
            continue

        paper_metadata = {
            "key": key,
            "title": item.get("title", ""),
            "authors": ", ".join(item.get("authors", [])),
            "year": str(item.get("year", "")),
            "doi": item.get("doi", ""),
            "url": item.get("url", ""),
            "abstract": item.get("abstract", ""),
        }
        chunks = chunker.chunk_markdown(markdown_text, paper_metadata=paper_metadata)
        if not chunks:
            logger.warning(f"  切块为空，跳过")
            continue

        target_collections = item.get("collection_names", [config.DEFAULT_COLLECTION])
        for col_name in target_collections:
            n = vector_store.add_chunks(chunks, collection_name=col_name)
            total_chunks += n

    logger.info(f"补全完毕！共处理 {len(missing)} 篇，{total_chunks} chunks")
    # Print final statistics
    final = vector_store.list_collections()
    logger.info("当前 ChromaDB 状态:")
    for col in final:
        logger.info(f"  {col['name']}: {col['count']} chunks")


if __name__ == "__main__":
    main()
