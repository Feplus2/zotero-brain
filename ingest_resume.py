# -*- coding: utf-8 -*-
"""
增量补全入库 —— 将 parsed/ 中已解析但未入库的论文入 ChromaDB

跳过了 PDF 下载和 MinerU 解析（已有 MD 缓存），
只做 chunk → embed → vector_store
"""
import logging

import config
import chunker
import pdf_parser
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

    # 3. Per-collection key sets; a paper is "missing" for each collection it
    #    belongs to (per Zotero collection_names) but hasn't been ingested into.
    #    （旧逻辑把所有库的 key 聚成一个大集合：论文在 A 库成功、B 库失败时会被
    #    误判"已入库"而永远跳过——逐库判断才能发现这种部分缺失。）
    col_keys: dict[str, set] = {}
    for col in vector_store.list_collections():
        col_keys[col["name"]] = vector_store.get_paper_keys(col["name"])
    logger.info(f"ChromaDB 共 {len(col_keys)} 个 collection")

    def _missing_collections(it: dict) -> list[str]:
        targets = it.get("collection_names") or [config.DEFAULT_COLLECTION]
        return [c for c in targets if it["key"] not in col_keys.get(c, set())]

    missing: dict[str, list[str]] = {}  # key -> 缺失的 collection 列表
    for key in sorted(parsed_keys):
        it = item_map.get(key)
        if it is None:
            continue
        mc = _missing_collections(it)
        if mc:
            missing[key] = mc
    logger.info(f"待补全: {len(missing)} 篇")
    if not missing:
        logger.info("全部入库完毕，无需补全！")
        return

    # 4. Backfill one by one（单篇 try/except：一篇失败不中断全局；只补缺失的 collection）
    total_chunks = 0
    failed = 0
    for i, (key, missing_cols) in enumerate(missing.items(), 1):
        try:
            md_path = parsed_dir / key / f"{key}.md"
            if not md_path.exists():
                logger.warning(f"  [{i}/{len(missing)}] {key}: MD 文件不存在，跳过")
                continue

            item = item_map[key]
            title = item.get("title", "?")[:60]
            logger.info(f"[{i}/{len(missing)}] {key}: {title} -> {missing_cols}")

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
                "journal": item.get("journal", ""),
                "volume": item.get("volume", ""),
                "issue": item.get("issue", ""),
                "pages": item.get("pages", ""),
            }
            content_list = pdf_parser.load_content_list(key)
            chunks = chunker.chunk_auto(markdown_text, content_list=content_list, paper_metadata=paper_metadata)
            if not chunks:
                logger.warning(f"  切块为空，跳过")
                continue

            for col_name in missing_cols:
                result = vector_store.add_chunks(chunks, collection_name=col_name)
                total_chunks += result["added"]
        except Exception as e:
            failed += 1
            logger.error(f"  {key}: 补全失败: {e}", exc_info=True)

    logger.info(f"补全完毕！共处理 {len(missing)} 篇（失败 {failed} 篇），{total_chunks} chunks")
    # Print final statistics
    final = vector_store.list_collections()
    logger.info("当前 ChromaDB 状态:")
    for col in final:
        logger.info(f"  {col['name']}: {col['count']} chunks")


if __name__ == "__main__":
    main()
