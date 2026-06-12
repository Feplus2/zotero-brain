# -*- coding: utf-8 -*-
"""
Zotero Sync - 从 Zotero 文献库拉取数据

功能：
  - 连接 Zotero Web API
  - 列出所有 Collection（文件夹）
  - 列出论文元数据（标题、作者、年份、DOI 等）
  - 下载 PDF 附件
  - 自动翻页，拉取全部论文
"""

import shutil
import logging
import time
from pathlib import Path

import config
from pyzotero import zotero

logger = logging.getLogger(__name__)

# pyzotero single request limit
_PAGE_SIZE = 100

# Non-paper types, skip
_SKIP_TYPES = {"attachment", "note", "annotation"}


def _get_client() -> zotero.Zotero:
    """创建 Zotero 客户端"""
    return zotero.Zotero(
        config.ZOTERO_USER_ID,
        config.ZOTERO_LIBRARY_TYPE,
        config.ZOTERO_API_KEY,
    )


def list_collections(zot: zotero.Zotero | None = None) -> list[dict]:
    """
    列出 Zotero 中所有 Collection（文件夹）

    返回: [{"key": "ABC123", "name": "电池", "parent": ""}, ...]
    """
    if zot is None:
        zot = _get_client()

    collections = zot.collections()
    result = []
    for col in collections:
        data = col["data"]
        result.append({
            "key": data["key"],
            "name": data["name"],
            "parent": data.get("parentCollection", ""),
        })
    logger.info(f"找到 {len(result)} 个 Collection")
    return result


def _fetch_all_items(zot: zotero.Zotero, collection_key: str | None = None) -> list:
    """
    翻页拉取全部 items（pyzotero 单次最多 100 条）

    使用 Zotero API 的 start + limit 分页，直到拿完为止。
    """
    all_items = []
    start = 0

    while True:
        if collection_key:
            batch = zot.collection_items(collection_key, limit=_PAGE_SIZE, start=start)
        else:
            batch = zot.items(limit=_PAGE_SIZE, start=start)

        if not batch:
            break

        all_items.extend(batch)
        logger.info(f"  已拉取 {len(all_items)} 条 (本批 {len(batch)})")

        if len(batch) < _PAGE_SIZE:
            break  # 最后一页

        start += _PAGE_SIZE
        time.sleep(0.3)  # 避免触发 API 限速

    return all_items


def _item_has_pdf(zot: zotero.Zotero, item_key: str) -> bool:
    """
    检查某篇论文是否有 PDF 子附件

    Zotero 的 PDF 是以 child attachment 形式挂载的，
    需要调 children() API 查询。
    """
    try:
        children = zot.children(item_key)
        for child in children:
            data = child["data"]
            content_type = data.get("contentType", "")
            if content_type == "application/pdf":
                return True
    except Exception as e:
        logger.debug(f"查询子条目失败 ({item_key}): {e}")
    return False


def list_items(
    zot: zotero.Zotero | None = None,
    collection_key: str | None = None,
    check_pdf: bool = True,
) -> list[dict]:
    """
    列出全部论文元数据（自动翻页）

    Args:
        collection_key: 指定 Collection 的 key，None 表示全部
        check_pdf: 是否逐篇检查有无 PDF（会额外调 children API）

    返回: [{
        "key": "ABC123",
        "title": "固态电解质...",
        "authors": ["Wang", "Li"],
        "year": 2024,
        "doi": "10.1038/...",
        "item_type": "journalArticle",
        "collection_names": ["钠电层状氧化物正极"],
        "has_pdf": True,
        "abstract": "...",
    }, ...]
    """
    if zot is None:
        zot = _get_client()

    # Pull Collection mapping first
    col_map = {}
    for col in list_collections(zot):
        col_map[col["key"]] = col["name"]

    # Paginate to fetch all items
    raw_items = _fetch_all_items(zot, collection_key)

    result = []
    total = len(raw_items)
    for idx, item in enumerate(raw_items):
        data = item["data"]

        # Skip non-paper types
        if data["itemType"] in _SKIP_TYPES:
            continue

        # Extract authors
        authors = []
        for creator in data.get("creators", []):
            if creator.get("creatorType") == "author":
                last = creator.get("lastName", "")
                first = creator.get("firstName", "")
                name = f"{last} {first}".strip()
                if name:
                    authors.append(name)

        # Extract year
        date_str = data.get("date", "")
        year = None
        if date_str:
            try:
                year = int(date_str[:4])
            except (ValueError, IndexError):
                pass

        # Determine Collection - use Zotero folder name directly
        collection_names = []
        for col_key in data.get("collections", []):
            col_name = col_map.get(col_key, "")
            if col_name:
                collection_names.append(col_name)

        if not collection_names:
            collection_names = [config.DEFAULT_COLLECTION]

        # Check for PDF attachments (via child items)
        has_pdf = False
        if check_pdf:
            has_pdf = _item_has_pdf(zot, data["key"])
            if idx % 20 == 0 and idx > 0:
                logger.info(f"  PDF 检查进度: {idx}/{total}")
            time.sleep(0.1)  # 避免 API 限速

        result.append({
            "key": data["key"],
            "title": data.get("title", ""),
            "authors": authors,
            "year": year,
            "doi": data.get("DOI", ""),
            "item_type": data["itemType"],
            "url": data.get("url", ""),
            "abstract": data.get("abstractNote", ""),
            "collection_names": collection_names,
            "has_pdf": has_pdf,
        })

    logger.info(f"找到 {len(result)} 篇论文")
    return result


def get_item_pdf_keys(zot: zotero.Zotero | None = None, item_key: str = "") -> list[str]:
    """获取某个论文下所有 PDF 附件的 key"""
    if zot is None:
        zot = _get_client()

    children = zot.children(item_key)
    pdf_keys = []
    for child in children:
        data = child["data"]
        content_type = data.get("contentType", "")
        if content_type == "application/pdf":
            pdf_keys.append(data["key"])
    return pdf_keys


def download_pdf(
    zot: zotero.Zotero | None = None,
    item_key: str = "",
    save_dir: Path | None = None,
) -> Path | None:
    """
    获取论文的 PDF 附件

    优先从本地 Zotero storage 复制（速度快、不依赖云端同步），
    本地找不到时才尝试 API 下载。

    Args:
        item_key: 论文的 Zotero key
        save_dir: 保存目录，默认 parsed/{item_key}/

    返回: PDF 文件路径，或 None（获取失败）
    """
    if zot is None:
        zot = _get_client()

    if save_dir is None:
        save_dir = config.PARSED_DIR / item_key
    save_dir.mkdir(parents=True, exist_ok=True)

    pdf_keys = get_item_pdf_keys(zot, item_key)
    if not pdf_keys:
        logger.warning(f"论文 {item_key} 没有 PDF 附件")
        return None

    pdf_key = pdf_keys[0]  # 取第一个 PDF
    pdf_path = save_dir / f"{item_key}.pdf"

    if pdf_path.exists():
        logger.info(f"PDF 已存在: {pdf_path}")
        return pdf_path

    # === Strategy 1: Copy from local Zotero storage ===
    local_dir = config.ZOTERO_LOCAL_STORAGE / pdf_key
    if local_dir.is_dir():
        # Find the first .pdf file in the directory
        for f in local_dir.iterdir():
            if f.suffix.lower() == ".pdf":
                shutil.copy2(f, pdf_path)
                logger.info(f"PDF 已从本地复制: {f.name} → {pdf_path}")
                return pdf_path

    # === Strategy 2: Fallback to API download (only works for cloud-synced attachments) ===
    try:
        zot.dump(pdf_key, str(save_dir))
        for f in save_dir.iterdir():
            if f.suffix == ".pdf" and f.name != pdf_path.name:
                f.rename(pdf_path)
                break
        if pdf_path.exists():
            logger.info(f"PDF 已从云端下载: {pdf_path}")
            return pdf_path
    except Exception as e:
        logger.debug(f"API 下载也失败: {e}")

    logger.error(f"无法获取 PDF: {item_key} (本地 {local_dir} 不存在，云端 404)")
    return None


def get_item_fulltext(zot: zotero.Zotero | None = None, item_key: str = "") -> str:
    """
    获取论文的全文内容（Zotero 内置的全文索引）

    注意：这只是 Zotero 自己的全文索引，质量有限。
    对于扫描版 PDF，需要走 MinerU 解析。
    """
    if zot is None:
        zot = _get_client()

    try:
        content = zot.fulltext_item(item_key)
        return content.get("content", "")
    except Exception:
        return ""


def sync_all(
    download_pdfs: bool = False,
) -> dict:
    """
    完整同步：拉取全部论文元数据 + 可选下载 PDF

    返回: {
        "collections": [...],
        "items": [...],
        "stats": {"total": 150, "with_pdf": 120, "no_pdf": 30},
        "by_collection": {"钠电层状氧化物正极": 80, ...},
    }
    """
    zot = _get_client()

    collections = list_collections(zot)
    items = list_items(zot, check_pdf=True)

    # Statistics
    with_pdf = sum(1 for i in items if i["has_pdf"])
    by_collection = {}
    for item in items:
        for cn in item["collection_names"]:
            by_collection[cn] = by_collection.get(cn, 0) + 1

    pdfs_downloaded = 0
    pdfs_failed = 0

    if download_pdfs:
        for item in items:
            if not item["has_pdf"]:
                continue
            result = download_pdf(zot, item["key"])
            if result:
                pdfs_downloaded += 1
            else:
                pdfs_failed += 1

    return {
        "collections": collections,
        "items": items,
        "stats": {
            "total": len(items),
            "with_pdf": with_pdf,
            "no_pdf": len(items) - with_pdf,
        },
        "by_collection": by_collection,
        "pdfs_downloaded": pdfs_downloaded,
        "pdfs_failed": pdfs_failed,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=== Zotero 文献库同步 ===\n")

    # List Collections
    collections = list_collections()
    print(f"Collection 列表 ({len(collections)}):")
    for col in collections:
        print(f"  - {col['name']}")

    print()

    # Fetch all papers (skip PDF check, quick count first)
    items = list_items(check_pdf=False)
    print(f"论文总数: {len(items)}\n")

    # Count by Collection
    by_col = {}
    for item in items:
        for cn in item["collection_names"]:
            by_col[cn] = by_col.get(cn, 0) + 1
    print("按 Collection 分布:")
    for name, count in sorted(by_col.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count} 篇")

    print()

    # Show first 20 papers
    print("前 20 篇:")
    for i, item in enumerate(items[:20]):
        year = item["year"] or "?"
        authors = ", ".join(item["authors"][:2]) if item["authors"] else "Unknown"
        cols = ", ".join(item["collection_names"][:2])
        title = item["title"][:60]
        print(f"  {i+1:3d}. [{year}] {title} | {authors} | {cols}")

    if len(items) > 20:
        print(f"  ... 还有 {len(items) - 20} 篇")
