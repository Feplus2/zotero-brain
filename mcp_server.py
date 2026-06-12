# -*- coding: utf-8 -*-
"""
MCP Server - expose Zotero Brain to WorkBuddy.

Phase 4: 工具解耦 + Zotero-First 设计
Tools provided (11):
  - search_papers: semantic search in library (supports paper_keys filter)
  - discover_papers: discover new papers from academic databases
  - download_paper: 6-level cascade PDF download (pure download, no Zotero/ChromaDB)
  - import_to_zotero: import PDF + metadata to Zotero (pure Zotero operation)
  - ingest_paper: parse PDF → chunk → embed → ChromaDB
  - list_collections: Zotero folders + ChromaDB collections + sync status
  - create_collection: create Zotero folder + ChromaDB collection simultaneously
  - get_bibtex: generate BibTeX (exact mode + semantic recommend mode)
  - get_paper_chunks: list paper chunk structure
  - expand_context: context expansion around a chunk
  - read_paper_full: read full paper text
"""

import logging
import re
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
)

import config
import zotero_sync
import pdf_parser
import chunker
import vector_store
import paper_discovery
import paper_importer
import network_helper

logger = logging.getLogger(__name__)

# Install MinerU TUN direct-connect patch (bypass TUN for MinerU domestic traffic)
network_helper.install()

# MCP Server instance
server = Server("zotero-brain")


# ============================================================================
# Internal: ingest a single paper
# ============================================================================

def _ingest_paper(
    item: dict,
    force_parse: bool = False,
    pdf_path: str | None = None,
    collection: str | None = None,
) -> int:
    """Ingest a single paper, return chunk count.

    Args:
        item: paper metadata dict (must have "key", "title", etc.)
        force_parse: force re-parse PDF
        pdf_path: known PDF path (skip Zotero download)
        collection: target collection name (overrides item's collection_names)
    """
    from pathlib import Path as _Path
    key = item["key"]
    title = item.get("title", "?")
    logger.info(f"[ingest] {key}: {title[:60]}")

    # 1. Get PDF
    if pdf_path:
        pdf_path = _Path(pdf_path)
        logger.info(f"  using existing PDF: {pdf_path}")
    else:
        pdf_path = zotero_sync.download_pdf(item_key=key)
    if pdf_path is None:
        logger.warning(f"  skip: no PDF")
        return 0

    # 2. MinerU parse
    markdown_text = pdf_parser.parse_pdf(pdf_path, item_key=key, force=force_parse)
    if not markdown_text.strip():
        logger.warning(f"  skip: empty parse result")
        return 0

    # 3. Chunk
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
        return 0

    # 4. Store in ChromaDB
    if collection:
        target_collections = [collection]
    else:
        target_collections = item.get("collection_names", [config.DEFAULT_COLLECTION])
    total = 0
    for col_name in target_collections:
        total += vector_store.add_chunks(chunks, collection_name=col_name)

    return total


def _generate_bibtex(meta: dict) -> str:
    """Generate BibTeX from metadata dict (supports full fields from Zotero)."""
    authors = meta.get("authors", [])
    if isinstance(authors, list):
        author_str = " and ".join(authors) if authors else "Unknown"
    else:
        author_str = str(authors) if authors else "Unknown"

    first_author = authors[0].split()[0] if authors else "unknown"
    year = meta.get("year", "")
    cite_key = f"{first_author}_{year}".lower().replace(" ", "_")

    lines = [f"@article{{{cite_key},"]
    lines.append(f"  title={{{{{meta.get('title', 'Unknown')}}}}},")
    lines.append(f"  author={{{{{author_str}}}}},")
    lines.append(f"  year={{{{{year}}}}},")
    if meta.get("doi"):
        lines.append(f"  doi={{{{{meta['doi']}}}}},")
    if meta.get("journal"):
        lines.append(f"  journal={{{{{meta['journal']}}}}},")
    if meta.get("volume"):
        lines.append(f"  volume={{{{{meta['volume']}}}}},")
    if meta.get("pages"):
        lines.append(f"  pages={{{{{meta['pages']}}}}},")
    if meta.get("issue"):
        lines.append(f"  number={{{{{meta['issue']}}}}},")
    if meta.get("url"):
        lines.append(f"  url={{{{{meta['url']}}}}},")
    lines.append("}")

    return "\n".join(lines)


# ============================================================================
# Tool definitions
# ============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools."""
    return [
        # === 搜索 ===
        Tool(
            name="search_papers",
            description="在 Zotero 文献库中语义搜索论文。支持跨 Collection 或指定领域搜索。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询（自然语言）",
                    },
                    "collections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "指定搜索的 Collection 列表（如 ['钠电层状氧化物正极']），留空表示搜索全部",
                    },
                    "paper_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定在某篇或某几篇论文内搜索（Zotero key 列表）",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "返回结果数量",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="discover_papers",
            description="从学术数据库（OpenAlex / arXiv / CrossRef / Semantic Scholar）搜索真实论文。返回候选列表，包含标题、DOI、引用数、是否有开放获取 PDF，以及是否已在你的文献库中。OpenAlex 为主力（2.4亿+论文，免费）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（英文）",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["openalex", "arxiv", "crossref", "semantic_scholar"]},
                        "description": "数据源（默认全部）。OpenAlex 为主力（免费无 key），其余为 fallback",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "每个源返回数量（默认 10）",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        # === 下载 + 导入 + 入库（解耦三件套）===
        Tool(
            name="download_paper",
            description="下载论文 PDF（6 级瀑布: 本地缓存 → OpenAlex OA → Unpaywall → CORE → arXiv → Sci-Hub）。纯下载，不碰 Zotero 不碰 ChromaDB。返回 PDF 本地路径 + 论文元数据。",
            inputSchema={
                "type": "object",
                "properties": {
                    "doi": {
                        "type": "string",
                        "description": "论文 DOI（如 '10.1038/nature12373'）。与 title 二选一，优先 doi。",
                    },
                    "title": {
                        "type": "string",
                        "description": "论文标题（用于搜索，如果只提供了 title 会先 discover 找到 DOI）",
                    },
                    "save_dir": {
                        "type": "string",
                        "description": "保存目录（可选，默认 data/downloads/）",
                    },
                },
            },
        ),
        Tool(
            name="import_to_zotero",
            description="将 PDF + metadata 导入 Zotero（创建条目 + linked_file 附件）。纯 Zotero 操作，不碰 ChromaDB。",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "论文标题（必填）",
                    },
                    "doi": {
                        "type": "string",
                        "description": "论文 DOI（可选）",
                    },
                    "authors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "作者列表（可选，格式: ['Last First', ...]）",
                    },
                    "year": {
                        "type": "integer",
                        "description": "发表年份（可选）",
                    },
                    "abstract": {
                        "type": "string",
                        "description": "摘要（可选）",
                    },
                    "url": {
                        "type": "string",
                        "description": "论文 URL（可选）",
                    },
                    "pdf_path": {
                        "type": "string",
                        "description": "本地 PDF 路径（可选，创建 linked_file 附件）",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Zotero 文件夹中文名（可选，如 '钠电层状氧化物正极'）",
                    },
                },
                "required": ["title"],
            },
        ),
        Tool(
            name="ingest_paper",
            description="解析 PDF → chunk → embed → ChromaDB 向量化入库。接受 Zotero key 或本地 pdf_path。",
            inputSchema={
                "type": "object",
                "properties": {
                    "zotero_key": {
                        "type": "string",
                        "description": "Zotero 论文 key（从 Zotero 拉 PDF + metadata）",
                    },
                    "pdf_path": {
                        "type": "string",
                        "description": "本地 PDF 路径（跳过 Zotero 下载，直接使用本地文件）",
                    },
                    "collection": {
                        "type": "string",
                        "description": "目标 Collection 中文名（可选，如 '钠电层状氧化物正极'）",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "是否强制重新解析",
                        "default": False,
                    },
                },
            },
        ),
        # === Collection 管理 ===
        Tool(
            name="list_collections",
            description="同时返回 Zotero 文件夹列表 + ChromaDB collection 列表 + 同步状态。Agent 可据此判断哪些文件夹已同步、哪些需要 create_collection。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="create_collection",
            description="同时创建 Zotero 文件夹 + ChromaDB collection。Agent 提供中文名和英文 slug（ChromaDB 只接受 [a-z0-9._-]）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_name": {
                        "type": "string",
                        "description": "Zotero 文件夹中文名（如 '钠电层状氧化物正极'）",
                    },
                    "chroma_name": {
                        "type": "string",
                        "description": "ChromaDB 英文 slug（如 'sodium-layered-oxide-cathode'）。要求: 3-512 字符, [a-z0-9._-], 首尾 a-z0-9",
                    },
                },
                "required": ["folder_name", "chroma_name"],
            },
        ),
        # === 引用 ===
        Tool(
            name="get_bibtex",
            description="生成 BibTeX 引用。支持两种模式: (1) exact - 给 identifier 精确生成单篇 BibTeX（Zotero 优先，ChromaDB fallback）; (2) recommend - 给写作内容描述，语义搜索知识库推荐相关论文 + BibTeX（Agent 辅助写作用）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "论文标识（标题、DOI 或 Zotero key）。mode=exact 时必填。",
                    },
                    "query": {
                        "type": "string",
                        "description": "写作内容描述或关键词。mode=recommend 时必填。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["exact", "recommend"],
                        "description": "exact=精确引用（默认）, recommend=语义推荐",
                        "default": "exact",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "recommend 模式返回数量（默认 5）",
                        "default": 5,
                    },
                    "collections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "recommend 模式限定搜索的 Collection（可选）",
                    },
                },
            },
        ),
        # === 深度阅读（不变）===
        Tool(
            name="get_paper_chunks",
            description="获取某篇论文的所有 chunk 目录（编号、章节名、前120字摘要）。用于了解论文结构，精准定位要深入阅读的段落。不返回全文。",
            inputSchema={
                "type": "object",
                "properties": {
                    "paper_key": {
                        "type": "string",
                        "description": "Zotero 论文 key",
                    },
                    "collection": {
                        "type": "string",
                        "description": "指定 Collection 名称（可选，加速查找）",
                    },
                },
                "required": ["paper_key"],
            },
        ),
        Tool(
            name="expand_context",
            description="获取某个 chunk 及其前后 N 个 chunk 的完整文本。用于在 search_papers 定位到相关片段后，扩展上下文深入理解。类似 SageRead 的 ragContext。",
            inputSchema={
                "type": "object",
                "properties": {
                    "paper_key": {
                        "type": "string",
                        "description": "Zotero 论文 key",
                    },
                    "chunk_index": {
                        "type": "integer",
                        "description": "目标 chunk 的编号（从 get_paper_chunks 获取）",
                    },
                    "prev": {
                        "type": "integer",
                        "description": "向前扩展的 chunk 数量",
                        "default": 2,
                    },
                    "next": {
                        "type": "integer",
                        "description": "向后扩展的 chunk 数量",
                        "default": 2,
                    },
                    "collection": {
                        "type": "string",
                        "description": "指定 Collection 名称（可选，加速查找）",
                    },
                },
                "required": ["paper_key", "chunk_index"],
            },
        ),
        Tool(
            name="read_paper_full",
            description="读取某篇论文的完整 Markdown 文本（从解析缓存中读取，不重新解析 PDF）。用于需要高精确度、绕过嵌入模型直接在 LLM 上下文中阅读全文的场景。返回文本量较大，仅在精准讨论单篇论文时使用。",
            inputSchema={
                "type": "object",
                "properties": {
                    "paper_key": {
                        "type": "string",
                        "description": "Zotero 论文 key",
                    },
                },
                "required": ["paper_key"],
            },
        ),
    ]


# ============================================================================
# Tool implementations
# ============================================================================

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool calls. All exceptions are caught and returned as error messages to prevent MCP server crash."""
    try:
        return await _dispatch_tool(name, arguments)
    except Exception as e:
        logger.error(f"Tool {name} crashed: {type(e).__name__}: {e}", exc_info=True)
        return [TextContent(type="text", text=f"Tool {name} failed: {type(e).__name__}: {e}\n\nThis error has been logged. The MCP server is still running.")]


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Internal tool dispatcher. All exceptions are caught by the caller."""
    import asyncio as _asyncio

    # ====================================================================
    # search_papers (unchanged)
    # ====================================================================
    if name == "search_papers":
        query = arguments["query"]
        collections = arguments.get("collections")
        paper_keys = arguments.get("paper_keys")
        n_results = arguments.get("n_results", 5)

        results = vector_store.search(
            query,
            collection_names=collections,
            n_results=n_results,
            paper_keys=paper_keys,
        )

        if not results:
            return [TextContent(type="text", text="未找到相关论文")]

        output = []
        for i, r in enumerate(results, 1):
            meta = r["metadata"]
            output.append(f"{i}. **{meta.get('title', '?')}**")
            output.append(f"   - 作者: {meta.get('authors', '?')}")
            output.append(f"   - 年份: {meta.get('year', '?')}")
            output.append(f"   - 相似度: {r['score']:.3f}")
            output.append(f"   - Collection: {r['collection']}")
            output.append(f"   - 片段: {r['text'][:200]}...")
            output.append("")

        return [TextContent(type="text", text="\n".join(output))]

    # ====================================================================
    # discover_papers (unchanged)
    # ====================================================================
    elif name == "discover_papers":
        query = arguments["query"]
        sources = arguments.get("sources")
        limit = arguments.get("limit", 10)

        papers = await _asyncio.to_thread(paper_discovery.discover, query, sources=sources, limit=limit)

        if not papers:
            return [TextContent(type="text", text="未找到相关论文")]

        output = [f"## 论文搜索结果 (query: {query})\n"]
        for i, p in enumerate(papers, 1):
            in_lib = "✅ 已入库" if p.get("in_library") else "⬜ 未入库"
            oa_pdf = p.get("open_access_pdf") or "❌"
            output.append(f"{i}. **{p['title'][:80]}**")
            output.append(f"   - 作者: {', '.join(p.get('authors', [])[:4])}")
            output.append(f"   - 年份: {p.get('year', '?')} | 引用: {p.get('citation_count', '?')} | DOI: {p.get('doi', '?')}")
            output.append(f"   - 来源: {p['source']} | {in_lib}")
            output.append(f"   - OA PDF: {oa_pdf[:80] if oa_pdf != '❌' else '❌'}")
            output.append("")

        return [TextContent(type="text", text="\n".join(output))]

    # ====================================================================
    # download_paper (NEW)
    # ====================================================================
    elif name == "download_paper":
        doi = arguments.get("doi")
        title = arguments.get("title")
        save_dir_arg = arguments.get("save_dir")

        if not doi and not title:
            return [TextContent(type="text", text="需要提供 doi 或 title 参数")]

        from pathlib import Path as _Path

        save_dir = _Path(save_dir_arg) if save_dir_arg else config.DATA_DIR / "downloads"

        # If only title provided, discover first to find DOI
        paper = None
        if doi:
            paper = {
                "title": title or "Unknown",
                "doi": doi,
                "authors": [],
                "year": None,
                "abstract": "",
                "citation_count": None,
                "open_access_pdf": None,
                "source": "manual",
                "url": f"https://doi.org/{doi}",
            }
            # Try to enrich metadata from CrossRef
            try:
                import httpx
                def _fetch_crossref():
                    return httpx.get(
                        f"https://api.crossref.org/works/{doi}",
                        headers={"User-Agent": f"ZoteroBrain/1.0 (mailto:{config.UNPAYWALL_EMAIL})"},
                        timeout=15,
                    )
                resp = await _asyncio.to_thread(_fetch_crossref)
                if resp.status_code == 200:
                    data = resp.json().get("message", {})
                    paper["title"] = data.get("title", [title or "Unknown"])[0]
                    paper["abstract"] = data.get("abstract", "")
                    paper["url"] = data.get("URL", "")
                    authors = []
                    for a in data.get("author", []):
                        family = a.get("family", "")
                        given = a.get("given", "")
                        nm = f"{family} {given}".strip()
                        if nm:
                            authors.append(nm)
                    paper["authors"] = authors
                    pub = data.get("published", {})
                    date_parts = pub.get("date-parts", [[None]])
                    if date_parts and date_parts[0]:
                        paper["year"] = date_parts[0][0]
            except Exception as e:
                logger.warning(f"CrossRef metadata fetch failed: {e}")
        else:
            # Only title, discover to find DOI + metadata
            papers = await _asyncio.to_thread(paper_discovery.discover, title, limit=5)
            if papers:
                paper = papers[0]
            else:
                return [TextContent(type="text", text=f"未找到匹配的论文: {title}")]

        # Run download cascade
        pdf_path, dl_source = await _asyncio.to_thread(
            paper_importer.download_pdf, paper, save_dir
        )

        if pdf_path is None:
            return [TextContent(type="text", text=(
                f"PDF 下载失败（6 级瀑布全部失败）\n"
                f"论文: {paper.get('title', '?')[:80]}\n"
                f"DOI: {paper.get('doi', '?')}\n\n"
                f"手动下载链接:\n"
                f"- DOI: https://doi.org/{paper.get('doi', '')}\n"
                f"- Sci-Hub: https://sci-hub.se/{paper.get('doi', '')}"
            ))]

        import json
        result = {
            "pdf_path": str(pdf_path),
            "source": dl_source,
            "paper": {
                "title": paper.get("title", ""),
                "doi": paper.get("doi", ""),
                "authors": paper.get("authors", []),
                "year": paper.get("year"),
                "abstract": paper.get("abstract", ""),
                "url": paper.get("url", ""),
            },
        }
        return [TextContent(type="text", text=f"✅ PDF 下载成功\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```")]

    # ====================================================================
    # import_to_zotero (NEW)
    # ====================================================================
    elif name == "import_to_zotero":
        title = arguments["title"]
        doi = arguments.get("doi", "")
        authors = arguments.get("authors", [])
        year = arguments.get("year")
        abstract = arguments.get("abstract", "")
        url = arguments.get("url", "")
        pdf_path_str = arguments.get("pdf_path")
        collection = arguments.get("collection")

        from pathlib import Path as _Path

        paper = {
            "title": title,
            "doi": doi,
            "authors": authors,
            "year": year,
            "abstract": abstract,
            "url": url,
            "open_access_pdf": None,
            "source": "manual",
        }

        pdf_path = _Path(pdf_path_str) if pdf_path_str else None

        item_key = await _asyncio.to_thread(
            paper_importer.import_to_zotero, paper, pdf_path, collection
        )

        if item_key is None:
            return [TextContent(type="text", text=f"Zotero 导入失败: {title[:60]}")]

        linked = "✅ linked_file" if pdf_path and pdf_path.exists() else "❌ 无附件"
        col_info = f"\nCollection: {collection}" if collection else ""
        return [TextContent(type="text", text=f"✅ Zotero 导入成功\n- Key: {item_key}\n- 标题: {title[:60]}\n- 附件: {linked}{col_info}")]

    # ====================================================================
    # ingest_paper (REDO: + pdf_path + collection)
    # ====================================================================
    elif name == "ingest_paper":
        zotero_key = arguments.get("zotero_key")
        pdf_path_str = arguments.get("pdf_path")
        collection = arguments.get("collection")
        force = arguments.get("force", False)

        if not zotero_key and not pdf_path_str:
            return [TextContent(type="text", text="需要提供 zotero_key 或 pdf_path 参数")]

        def _do_ingest():
            if zotero_key:
                # From Zotero: pull metadata + download PDF
                _items = zotero_sync.list_items(check_pdf=False)
                _target = None
                for _item in _items:
                    if _item["key"] == zotero_key:
                        _target = _item
                        break
                if _target is None:
                    return None, 0
                return _target, _ingest_paper(_target, force_parse=force, pdf_path=pdf_path_str, collection=collection)
            else:
                # From local PDF only: construct minimal item
                from pathlib import Path as _Path
                _pdf = _Path(pdf_path_str)
                if not _pdf.exists():
                    return None, 0
                _target = {
                    "key": _pdf.stem,  # Use filename stem as key
                    "title": _pdf.stem,
                    "authors": [],
                    "year": None,
                    "doi": "",
                    "url": "",
                    "abstract": "",
                    "collection_names": [collection] if collection else [config.DEFAULT_COLLECTION],
                }
                return _target, _ingest_paper(_target, force_parse=force, pdf_path=pdf_path_str, collection=collection)

        target, added = await _asyncio.to_thread(_do_ingest)

        if target is None:
            if zotero_key:
                return [TextContent(type="text", text=f"未找到论文 (key={zotero_key})")]
            return [TextContent(type="text", text=f"PDF 不存在: {pdf_path_str}")]

        col_info = f" → {collection}" if collection else ""
        return [TextContent(type="text", text=f"✅ 入库完成: {target.get('title', '?')[:50]}\n添加 {added} 个文本块{col_info}")]

    # ====================================================================
    # list_collections (REDO: Zotero folders + ChromaDB + sync status)
    # ====================================================================
    elif name == "list_collections":
        def _fetch_all():
            zot = zotero_sync._get_client()
            zot_folders = zotero_sync.list_folders(zot)
            chroma_cols = vector_store.list_collections()
            return zot_folders, chroma_cols

        zot_folders, chroma_cols = await _asyncio.to_thread(_fetch_all)

        # Build sync status map
        name_map = config._NAME_MAP  # zh -> en mapping

        sync_status = {}
        for folder in zot_folders:
            fname = folder["name"]
            chroma_name = name_map.get(fname)
            synced = False
            chroma_count = 0
            if chroma_name:
                for cc in chroma_cols:
                    if cc["safe_name"] == chroma_name:
                        synced = True
                        chroma_count = cc["count"]
                        break
            sync_status[fname] = {
                "zotero_key": folder["key"],
                "zotero_item_count": folder["item_count"],
                "chroma_name": chroma_name,
                "chroma_chunks": chroma_count if synced else None,
                "synced": synced,
            }

        output = ["## Zotero 文件夹\n"]
        for f in zot_folders:
            s = sync_status[f["name"]]
            sync_icon = "✅" if s["synced"] else "⚠️ 未同步"
            output.append(f"- **{f['name']}** (key={f['key']}, {f['item_count']}篇) {sync_icon}")

        output.append(f"\n## ChromaDB Collections ({len(chroma_cols)})\n")
        for c in chroma_cols:
            output.append(f"- **{c['name']}** (`{c['safe_name']}`): {c['count']} chunks")

        unsynced = [f for f, s in sync_status.items() if not s["synced"]]
        if unsynced:
            output.append(f"\n⚠️ 以下 {len(unsynced)} 个 Zotero 文件夹未同步到 ChromaDB:")
            for f in unsynced:
                output.append(f"  - {f} → 请用 create_collection() 创建对应 ChromaDB collection")

        return [TextContent(type="text", text="\n".join(output))]

    # ====================================================================
    # create_collection (NEW)
    # ====================================================================
    elif name == "create_collection":
        folder_name = arguments["folder_name"]
        chroma_name = arguments["chroma_name"]

        # Validate chroma_name
        if not re.match(r'^[a-z0-9][a-z0-9._-]{1,510}[a-z0-9]$', chroma_name):
            return [TextContent(type="text", text=(
                f"❌ ChromaDB 名称 '{chroma_name}' 不合法。\n"
                f"要求: 3-512 字符, [a-z0-9._-], 首尾必须 a-z0-9\n"
                f"示例: 'sodium-layered-oxide-cathode'"
            ))]

        def _do_create():
            zot = zotero_sync._get_client()
            # 1. Create Zotero folder
            folder_key = zotero_sync.create_folder(folder_name, zot=zot)
            # 2. Create ChromaDB collection + register mapping
            vector_store.create_collection(folder_name, chroma_name, zotero_folder_key=folder_key)
            return folder_key

        try:
            folder_key = await _asyncio.to_thread(_do_create)
            return [TextContent(type="text", text=(
                f"✅ 已创建:\n"
                f"- Zotero 文件夹: {folder_name} (key={folder_key})\n"
                f"- ChromaDB: {chroma_name}\n"
                f"- 映射: {folder_name} ↔ {chroma_name}"
            ))]
        except ValueError as e:
            return [TextContent(type="text", text=f"❌ {e}")]
        except Exception as e:
            return [TextContent(type="text", text=f"❌ 创建失败: {type(e).__name__}: {e}")]

    # ====================================================================
    # get_bibtex (REDO: dual mode - exact + recommend)
    # ====================================================================
    elif name == "get_bibtex":
        mode = arguments.get("mode", "exact")
        identifier = arguments.get("identifier", "")
        query = arguments.get("query", "")
        n_results = arguments.get("n_results", 5)
        collections = arguments.get("collections")

        if mode == "recommend":
            # === Recommend mode: semantic search + BibTeX for each ===
            if not query:
                return [TextContent(type="text", text="recommend 模式需要提供 query 参数")]

            # Search more results to account for deduplication (multiple chunks per paper)
            raw_results = vector_store.search(
                query,
                collection_names=collections,
                n_results=n_results * 3,
            )

            if not raw_results:
                return [TextContent(type="text", text="未找到相关论文，无法推荐引用")]

            # Deduplicate by paper key, keep highest score per paper
            seen_keys = set()
            deduped = []
            for r in raw_results:
                paper_key = r["metadata"].get("key", "")
                if paper_key and paper_key in seen_keys:
                    continue
                if paper_key:
                    seen_keys.add(paper_key)
                deduped.append(r)
                if len(deduped) >= n_results:
                    break

            if not deduped:
                return [TextContent(type="text", text="未找到相关论文，无法推荐引用")]

            output = [f"## 语义引用推荐 (query: {query[:60]})\n"]
            for i, r in enumerate(deduped, 1):
                meta = r["metadata"]
                output.append(f"### {i}. {meta.get('title', '?')} (相似度: {r['score']:.3f})")
                output.append(f"- 作者: {meta.get('authors', '?')} | 年份: {meta.get('year', '?')}")
                bibtex = _generate_bibtex(meta)
                output.append(f"```bibtex\n{bibtex}\n```")
                output.append("")

            return [TextContent(type="text", text="\n".join(output))]

        else:
            # === Exact mode: Zotero API first, ChromaDB fallback, CrossRef fallback ===
            if not identifier:
                return [TextContent(type="text", text="exact 模式需要提供 identifier 参数（标题、DOI 或 Zotero key）")]

            # Try 1: Zotero API (most complete metadata)
            def _try_zotero():
                return zotero_sync.get_item_metadata(identifier)
            zot_meta = await _asyncio.to_thread(_try_zotero)

            if zot_meta and zot_meta.get("title"):
                bibtex = _generate_bibtex(zot_meta)
                return [TextContent(type="text", text=(
                    f"✅ BibTeX (来源: Zotero API)\n\n```bibtex\n{bibtex}\n```"
                ))]

            # Try 2: ChromaDB metadata
            chroma_results = vector_store.search(identifier, n_results=1)
            if chroma_results:
                meta = chroma_results[0]["metadata"]
                bibtex = _generate_bibtex(meta)
                return [TextContent(type="text", text=(
                    f"✅ BibTeX (来源: ChromaDB 知识库)\n\n```bibtex\n{bibtex}\n```"
                ))]

            # Try 3: CrossRef API (if identifier looks like a DOI)
            if identifier.startswith("10."):
                try:
                    import httpx
                    def _fetch_crossref():
                        return httpx.get(
                            f"https://api.crossref.org/works/{identifier}",
                            headers={"User-Agent": f"ZoteroBrain/1.0 (mailto:{config.UNPAYWALL_EMAIL})"},
                            timeout=15,
                        )
                    resp = await _asyncio.to_thread(_fetch_crossref)
                    if resp.status_code == 200:
                        data = resp.json().get("message", {})
                        cr_meta = {
                            "title": data.get("title", ["Unknown"])[0],
                            "authors": [],
                            "year": None,
                            "doi": data.get("DOI", ""),
                            "journal": data.get("container-title", [""])[0] if data.get("container-title") else "",
                            "volume": data.get("volume", ""),
                            "pages": data.get("page", ""),
                            "issue": data.get("issue", ""),
                            "url": data.get("URL", ""),
                        }
                        for a in data.get("author", []):
                            family = a.get("family", "")
                            given = a.get("given", "")
                            nm = f"{family} {given}".strip()
                            if nm:
                                cr_meta["authors"].append(nm)
                        pub = data.get("published", {})
                        date_parts = pub.get("date-parts", [[None]])
                        if date_parts and date_parts[0]:
                            cr_meta["year"] = date_parts[0][0]

                        bibtex = _generate_bibtex(cr_meta)
                        return [TextContent(type="text", text=(
                            f"✅ BibTeX (来源: CrossRef API)\n\n```bibtex\n{bibtex}\n```"
                        ))]
                except Exception as e:
                    logger.warning(f"CrossRef fallback failed: {e}")

            return [TextContent(type="text", text=f"❌ 未找到论文: {identifier}\n尝试了 Zotero API → ChromaDB → CrossRef，均无结果。")]

    # ====================================================================
    # get_paper_chunks (unchanged)
    # ====================================================================
    elif name == "get_paper_chunks":
        paper_key = arguments["paper_key"]
        collection = arguments.get("collection")

        chunks = vector_store.get_chunks_by_key(paper_key, collection_name=collection)

        if not chunks:
            return [TextContent(type="text", text=f"未找到论文 (key={paper_key}) 的 chunk。可能该论文尚未入库。")]

        output = [f"## 论文 Chunk 目录 (key={paper_key})\n"]
        for c in chunks:
            output.append(f"**[{c['chunk_index']}]** [{c['section']}] {c['summary']}")
            output.append("")

        return [TextContent(type="text", text="\n".join(output))]

    # ====================================================================
    # expand_context (unchanged)
    # ====================================================================
    elif name == "expand_context":
        paper_key = arguments["paper_key"]
        chunk_index = arguments["chunk_index"]
        prev = arguments.get("prev", 2)
        next_n = arguments.get("next", 2)
        collection = arguments.get("collection")

        context = vector_store.get_context(
            paper_key, chunk_index, prev=prev, next=next_n, collection_name=collection,
        )

        if not context:
            return [TextContent(type="text", text=f"未找到 chunk [{chunk_index}]，请检查 paper_key 和 chunk_index")]

        output = [f"## 上下文扩展: paper={paper_key}, anchor=[{chunk_index}]\n"]
        for c in context:
            marker = " ANCHOR" if c.get("is_anchor") else ""
            output.append(f"### [{c['chunk_index']}] {c['section']}{marker}\n")
            output.append(c["text"])
            output.append("")

        return [TextContent(type="text", text="\n".join(output))]

    # ====================================================================
    # read_paper_full (unchanged)
    # ====================================================================
    elif name == "read_paper_full":
        paper_key = arguments["paper_key"]

        full_text = vector_store.get_full_text(paper_key)
        if full_text is None:
            return [TextContent(type="text", text=f"未找到论文 {paper_key} 的解析缓存。请确认该论文已入库（parsed/ 目录下应有 {paper_key}.md）。")]

        char_count = len(full_text)
        output = f"## 论文全文 (key={paper_key}, {char_count} 字)\n\n{full_text}"

        return [TextContent(type="text", text=output)]

    else:
        return [TextContent(type="text", text=f"未知工具: {name}")]


# ============================================================================
# Server startup
# ============================================================================

async def main():
    """Start MCP Server."""
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Zotero Brain MCP Server starting (Phase 4: 11 tools)")
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import asyncio
    asyncio.run(main())
