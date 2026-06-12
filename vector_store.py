# -*- coding: utf-8 -*-
"""
Vector Store — ChromaDB 多集合管理
官方文档: docs.trychroma.com
命名规则: 3-512 字符, [a-z0-9._-], 首尾必须 a-z0-9
中文 display_name 存在 metadata 里
"""
import logging
import chromadb
from chromadb.config import Settings

import config
from chunker import Chunk
from embedder import embed_batch

logger = logging.getLogger(__name__)


def _client() -> chromadb.ClientAPI:
    return chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )


def _get_collection(client: chromadb.ClientAPI, name: str) -> chromadb.Collection:
    """获取或创建 Collection，中文名存入 metadata display_name"""
    safe_name = config.translate_collection_name(name)
    return client.get_or_create_collection(
        name=safe_name,
        metadata={"hnsw:space": "cosine", "display_name": name},
    )


def add_chunks(chunks: list[Chunk], collection_name: str) -> int:
    if not chunks:
        return 0
    client = _client()
    col = _get_collection(client, collection_name)

    ids = [f"{c.metadata.get('key', '?')}_{c.metadata.get('chunk_index', i)}"
           for i, c in enumerate(chunks)]
    docs = [c.text for c in chunks]
    metas = [c.metadata for c in chunks]

    logger.info(f"  向量化 {len(docs)} chunks...")
    vecs = embed_batch(docs)

    col.add(ids=ids, documents=docs, metadatas=metas, embeddings=vecs)
    logger.info(f"  ✓ {len(ids)} chunks → {col.name}")
    return len(ids)


def search(query: str, collection_names: list[str] | None = None,
           n_results: int = 10, paper_keys: list[str] | None = None) -> list[dict]:
    client = _client()

    if collection_names is None:
        cols = [c.name for c in client.list_collections()]
    else:
        cols = [config.translate_collection_name(n) for n in collection_names]

    if not cols:
        return []

    qvec = embed_batch([query])[0]

    # Build ChromaDB where filter
    where_filter = None
    if paper_keys:
        if len(paper_keys) == 1:
            where_filter = {"key": paper_keys[0]}
        else:
            where_filter = {"key": {"$in": paper_keys}}

    all_res = []
    for name in cols:
        try:
            col = client.get_collection(name)
        except Exception:
            continue
        r = col.query(query_embeddings=[qvec], n_results=n_results,
                      where=where_filter,
                      include=["documents", "metadatas", "distances"])
        if r["ids"] and r["ids"][0]:
            for i, did in enumerate(r["ids"][0]):
                all_res.append({
                    "text": r["documents"][0][i],
                    "metadata": r["metadatas"][0][i],
                    "score": 1 - r["distances"][0][i],
                    "collection": config.get_display_name(name),
                })
    all_res.sort(key=lambda x: x["score"], reverse=True)
    return all_res[:n_results]


def list_collections() -> list[dict]:
    client = _client()
    return [
        {"name": config.get_display_name(c.name), "safe_name": c.name,
         "count": c.count()}
        for c in client.list_collections()
    ]


def get_paper_keys(collection_name: str) -> set[str]:
    safe = config.translate_collection_name(collection_name)
    client = _client()
    col = _get_collection(client, collection_name)
    r = col.get(include=["metadatas"])
    return {m["key"] for m in r["metadatas"] if "key" in m}


def exists_by_metadata(field: str, value: str) -> bool:
    """
    在任意 collection 中检查是否存在 metadata 字段精确匹配的条目。
    用于去重判断（DOI / title 匹配），比语义搜索快且准。
    """
    client = _client()
    for c in client.list_collections():
        try:
            r = c.get(where={field: value}, include=[], limit=1)
            if r["ids"] and r["ids"][0]:
                return True
        except Exception:
            continue
    return False


def delete_paper(paper_key: str, collection_name: str) -> int:
    col = _get_collection(_client(), collection_name)
    r = col.get(where={"key": paper_key})
    if r["ids"]:
        col.delete(ids=r["ids"])
        return len(r["ids"])
    return 0


def _find_col_for_paper(paper_key: str, client: chromadb.ClientAPI) -> chromadb.Collection | None:
    """在任意 collection 中找到包含某篇论文的 collection"""
    for c in client.list_collections():
        r = c.get(where={"key": paper_key}, include=["metadatas"])
        if r["ids"] and r["ids"][0]:
            return c
    return None


def get_chunks_by_key(paper_key: str, collection_name: str | None = None) -> list[dict]:
    """
    获取某篇论文的所有 chunk，按 chunk_index 排序。

    返回: [{"chunk_index": int, "section": str, "summary": str}, ...]
    summary 是前 120 字，用于预览而非全文。
    """
    client = _client()

    if collection_name:
        cols = [client.get_collection(config.translate_collection_name(collection_name))]
    else:
        col = _find_col_for_paper(paper_key, client)
        if col is None:
            return []
        cols = [col]

    results = []
    for col in cols:
        r = col.get(
            where={"key": paper_key},
            include=["documents", "metadatas"],
        )
        if not r["ids"]:
            continue

        # Pair and sort
        paired = []
        for i, doc in enumerate(r["documents"]):
            meta = r["metadatas"][i]
            paired.append((meta.get("chunk_index", 0), doc, meta))
        paired.sort(key=lambda x: x[0])

        for chunk_idx, doc, meta in paired:
            results.append({
                "chunk_index": chunk_idx,
                "section": meta.get("section", ""),
                "summary": doc[:120].replace("\n", " "),
                "collection": config.get_display_name(col.name),
            })

    return results


def get_context(
    paper_key: str,
    chunk_index: int,
    prev: int = 2,
    next: int = 2,
    collection_name: str | None = None,
) -> list[dict]:
    """
    获取某个 chunk 及其前后 N 个 chunk 的完整文本。

    返回: [{"chunk_index": int, "section": str, "text": str}, ...]
    """
    client = _client()

    if collection_name:
        col = client.get_collection(config.translate_collection_name(collection_name))
    else:
        col = _find_col_for_paper(paper_key, client)
        if col is None:
            return []

    r = col.get(
        where={"key": paper_key},
        include=["documents", "metadatas"],
    )
    if not r["ids"]:
        return []

    # Sort by chunk_index
    paired = []
    for i, doc in enumerate(r["documents"]):
        meta = r["metadatas"][i]
        paired.append((meta.get("chunk_index", 0), doc, meta))
    paired.sort(key=lambda x: x[0])

    # Find the position of the target chunk in the sorted list
    target_pos = None
    for pos, (idx, _, _) in enumerate(paired):
        if idx == chunk_index:
            target_pos = pos
            break

    if target_pos is None:
        return []

    # Slice
    start = max(0, target_pos - prev)
    end = min(len(paired), target_pos + next + 1)
    sliced = paired[start:end]

    return [
        {
            "chunk_index": idx,
            "section": meta.get("section", ""),
            "text": doc,
            "is_anchor": idx == chunk_index,
        }
        for idx, doc, meta in sliced
    ]


def get_full_text(paper_key: str) -> str | None:
    """
    从 parsed/ 缓存读取论文的完整 Markdown 文本。
    不重新解析 PDF。
    MinerU 输出结构: parsed/{key}/{stem}.md (stem may differ from key)
    """
    candidates = [
        config.PARSED_DIR / paper_key / f"{paper_key}.md",  # MinerU 标准输出
        config.PARSED_DIR / f"{paper_key}.md",               # 备选
        config.PARSED_DIR / paper_key,                        # 备选（无扩展名文件）
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p.read_text("utf-8")

    # Fallback: scan parsed/{key}/ for any .md file
    key_dir = config.PARSED_DIR / paper_key
    if key_dir.is_dir():
        md_files = list(key_dir.glob("*.md"))
        if md_files:
            return md_files[0].read_text("utf-8")

    return None


def create_collection(
    collection_name: str,
    chroma_name: str,
    zotero_folder_key: str = "",
) -> str:
    """
    创建 ChromaDB Collection，同时注册中英文映射 + 写入 Zotero 关联 metadata。

    Args:
        collection_name: 中文名（如 "钠电层状氧化物正极"）
        chroma_name: ChromaDB 安全名（如 "sodium-layered-oxide-cathode"）
        zotero_folder_key: 对应 Zotero Collection key（可选）

    Returns: ChromaDB collection name
    """
    # 注册映射（会校验 chroma_name 合法性）
    config.register_collection_mapping(collection_name, chroma_name)

    client = _client()
    metadata = {
        "hnsw:space": "cosine",
        "display_name": collection_name,
    }
    if zotero_folder_key:
        metadata["zotero_folder_key"] = zotero_folder_key

    col = client.get_or_create_collection(name=chroma_name, metadata=metadata)
    logger.info(f"ChromaDB Collection 已创建: {chroma_name} (display={collection_name})")
    return col.name


def get_collection_metadata(chroma_name: str) -> dict | None:
    """获取 Collection 的 metadata（display_name, zotero_folder_key 等）"""
    client = _client()
    try:
        col = client.get_collection(chroma_name)
        return col.metadata
    except Exception:
        return None
