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
try:
    network_helper.install()
except Exception as e:
    logger.warning(f"Network helper install failed (MinerU direct-connect may not work): {e}")

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
) -> dict:
    """Ingest a single paper, return structured result dict.

    Args:
        item: paper metadata dict (must have "key", "title", etc.)
        force_parse: force re-parse PDF
        pdf_path: known PDF path (skip Zotero download)
        collection: target collection name (overrides item's collection_names)

    Returns: {"added": int, "skipped": int, "skipped_details": [...]}
    """
    from pathlib import Path as _Path
    key = item["key"]
    title = item.get("title", "?")
    logger.info(f"[ingest] {key}: {title[:60]}")

    # 0. Skip if already ingested in ChromaDB (avoid wasteful re-embedding)
    if not force_parse:
        existing_chunks = vector_store.get_chunks_by_key(key, collection_name=collection)
        if existing_chunks:
            logger.info(f"  already in ChromaDB ({len(existing_chunks)} chunks), skipping")
            return {"added": 0, "skipped": 0, "skipped_details": []}

    # 1. Get PDF
    if pdf_path:
        pdf_path = _Path(pdf_path)
        logger.info(f"  using existing PDF: {pdf_path}")
    else:
        pdf_path = zotero_sync.download_pdf(item_key=key)
    if pdf_path is None:
        logger.warning(f"  skip: no PDF")
        return {"added": 0, "skipped": 0, "skipped_details": []}

    # 1.5 Auto-archive PDF to permanent storage (safe mode: don't rename linked files)
    _original_pdf_path = pdf_path
    pdf_path = paper_importer._archive_pdf(pdf_path, item, allow_rename=False)
    # Update Zotero linked_file only if no attachment exists yet (avoid redundant API calls)
    if pdf_path != _original_pdf_path:
        existing = zotero_sync._get_linked_file_path(None, key)
        if not existing:
            zotero_sync.update_linked_file_path(key, str(pdf_path.resolve()))

    # 2. MinerU parse
    markdown_text = pdf_parser.parse_pdf(pdf_path, item_key=key, force=force_parse)
    if not markdown_text.strip():
        logger.warning(f"  skip: empty parse result")
        return {"added": 0, "skipped": 0, "skipped_details": []}

    # 3. Chunk
    paper_metadata = {
        "key": key,
        "title": title,
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
    chunks = chunker.chunk_markdown(markdown_text, paper_metadata=paper_metadata)
    if not chunks:
        return {"added": 0, "skipped": 0, "skipped_details": []}

    # 4. Store in ChromaDB
    if collection:
        target_collections = [collection]
    else:
        target_collections = item.get("collection_names", [config.DEFAULT_COLLECTION])
    total_result = {"added": 0, "skipped": 0, "skipped_details": []}
    for col_name in target_collections:
        config.ensure_collection_mapping(col_name)
        result = vector_store.add_chunks(chunks, collection_name=col_name)
        total_result["added"] += result["added"]
        total_result["skipped"] += result["skipped"]
        for sd in result["skipped_details"]:
            total_result["skipped_details"].append({**sd, "collection": col_name})

    return total_result


def _generate_bibtex(meta: dict) -> str:
    """Generate BibTeX from metadata dict (supports full fields from Zotero)."""
    authors = meta.get("authors", [])
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]
    if isinstance(authors, list):
        author_str = " and ".join(authors) if authors else "Unknown"
    else:
        author_str = str(authors) if authors else "Unknown"

    first_author = authors[0].split()[0] if authors else "unknown"
    year = meta.get("year", "")
    cite_key = f"{first_author}_{year}".lower().replace(" ", "_")

    lines = [f"@article{{{cite_key},"]
    lines.append(f"  title={{{meta.get('title', 'Unknown')}}},")
    lines.append(f"  author={{{author_str}}},")
    lines.append(f"  year={{{year}}},")
    if meta.get("doi"):
        lines.append(f"  doi={{{meta['doi']}}},")
    if meta.get("journal"):
        lines.append(f"  journal={{{meta['journal']}}},")
    if meta.get("volume"):
        lines.append(f"  volume={{{meta['volume']}}},")
    if meta.get("pages"):
        lines.append(f"  pages={{{meta['pages']}}},")
    if meta.get("issue"):
        lines.append(f"  number={{{meta['issue']}}},")
    if meta.get("url"):
        lines.append(f"  url={{{meta['url']}}},")
    lines.append("}")

    return "\n".join(lines)


# ============================================================================
# Parameter validation
# ============================================================================

_VALIDATE_MAX_RESULTS = 100
_VALIDATE_MAX_CONTEXT = 20


def _validate_tool_args(name: str, args: dict) -> str | None:
    """Validate tool arguments. Returns error message string, or None if valid.

    Strategy: numeric params are clamped (not rejected) to safe bounds;
    type errors on critical params return an error message.
    """
    # --- truncation bounds (clamp, don't reject) ---
    if "n_results" in args:
        n = args["n_results"]
        if isinstance(n, (int, float)):
            args["n_results"] = min(max(int(n), 1), _VALIDATE_MAX_RESULTS)
        else:
            args["n_results"] = 5

    if "limit" in args:
        li = args["limit"]
        if isinstance(li, (int, float)):
            args["limit"] = min(max(int(li), 1), _VALIDATE_MAX_RESULTS)
        else:
            args["limit"] = 10

    if "prev" in args:
        args["prev"] = max(min(int(args.get("prev", 2)), _VALIDATE_MAX_CONTEXT), 0)

    if "next" in args:
        args["next"] = max(min(int(args.get("next", 2)), _VALIDATE_MAX_CONTEXT), 0)

    # --- type coercion ---
    if "chunk_index" in args:
        ci = args["chunk_index"]
        if isinstance(ci, str) and ci.lstrip("-").isdigit():
            args["chunk_index"] = int(ci)
        if not isinstance(args["chunk_index"], int):
            return "chunk_index 必须是整数"

    # --- non-empty checks ---
    if "query" in args:
        q = (args.get("query") or "").strip()
        if not q:
            return "query 不能为空"
        args["query"] = q[:500]

    if "title" in args:
        t = (args.get("title") or "").strip()
        if not t:
            return "title 为必填参数"
        args["title"] = t

    if "folder_name" in args:
        fn = (args.get("folder_name") or "").strip()
        if not fn:
            return "folder_name 不能为空"
        args["folder_name"] = fn

    # --- ingest: verify file exists ---
    if name == "ingest_paper":
        pdf_path_str = args.get("pdf_path")
        zotero_key = args.get("zotero_key")
        batch_col = (args.get("batch_collection") or "").strip()
        if batch_col:
            return None  # batch_collection mode: skip individual checks
        if not zotero_key and not pdf_path_str:
            return "需要提供 zotero_key、pdf_path 或 batch_collection"
        if pdf_path_str:
            from pathlib import Path
            if not Path(pdf_path_str).exists():
                return f"PDF 文件不存在: {pdf_path_str}"

    return None


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
                    "journal": {
                        "type": "string",
                        "description": "期刊名（可选）",
                    },
                    "volume": {
                        "type": "string",
                        "description": "卷号（可选）",
                    },
                    "issue": {
                        "type": "string",
                        "description": "期号（可选）",
                    },
                    "pages": {
                        "type": "string",
                        "description": "页码（可选）",
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
            description="解析 PDF → chunk → embed → ChromaDB 向量化入库。接受 Zotero key、本地 pdf_path，或 batch_collection（按 Zotero 文件夹批量入库）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "zotero_key": {
                        "type": "string",
                        "description": "Zotero 论文 key（从 Zotero 拉 PDF + metadata）。与 pdf_path、batch_collection 三选一。",
                    },
                    "pdf_path": {
                        "type": "string",
                        "description": "本地 PDF 路径（跳过 Zotero 下载，直接使用本地文件）。与 zotero_key、batch_collection 三选一。",
                    },
                    "batch_collection": {
                        "type": "string",
                        "description": "Zotero 文件夹中文名（如 '钠电层状氧化物正极'）。提供时忽略 zotero_key 和 pdf_path，批量入库该文件夹下所有未入库论文。适合小文件夹（≤30 篇）。大会话可能超时，大批量建议用 CLI 的 run_ingest.py。",
                    },
                    "collection": {
                        "type": "string",
                        "description": "目标 Collection 中文名（可选）。未提供时沿用各论文在 Zotero 中的已有分类。批量模式建议保持一致，避免论文被重复解析。",
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
	    description="同时返回 Zotero 文件夹列表 + ChromaDB collection 列表 + 同步状态。注意：Zotero 条目数包含子条目（笔记、附件等），非纯论文数。Agent 可据此判断哪些文件夹已同步、哪些需要 create_collection。",
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

    err = _validate_tool_args(name, arguments)
    if err:
        return [TextContent(type="text", text=f"参数错误: {err}")]

    # ====================================================================
    # search_papers (unchanged)
    # ====================================================================
    if name == "search_papers":
        query = arguments["query"]
        collections = arguments.get("collections")
        paper_keys = arguments.get("paper_keys")
        n_results = arguments.get("n_results", 5)

        results = await _asyncio.to_thread(
            vector_store.search,
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
                "journal": "",
                "volume": "",
                "issue": "",
                "pages": "",
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
                    paper["journal"] = (data.get("container-title") or [""])[0] or ""
                    paper["volume"] = data.get("volume", "") or ""
                    paper["issue"] = data.get("issue", "") or ""
                    paper["pages"] = data.get("page", "") or ""
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
                "journal": paper.get("journal", ""),
                "volume": paper.get("volume", ""),
                "issue": paper.get("issue", ""),
                "pages": paper.get("pages", ""),
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
            "journal": arguments.get("journal", ""),
            "volume": arguments.get("volume", ""),
            "issue": arguments.get("issue", ""),
            "pages": arguments.get("pages", ""),
            "open_access_pdf": None,
            "source": "manual",
        }

        pdf_path = _Path(pdf_path_str) if pdf_path_str else None

        item_key = await _asyncio.to_thread(
            paper_importer.import_to_zotero, paper, pdf_path, collection
        )

        if item_key is None:
            return [TextContent(type="text", text=f"Zotero 导入失败: {title[:60]}")]

        # Check if PDF was archived (import_to_zotero moves pdf to data/papers/)
        has_pdf = False
        if pdf_path_str:
            archive_name = paper_importer._safe_filename(paper)
            archive_path = config.PAPERS_DIR / archive_name
            has_pdf = archive_path.exists()
        linked = "✅ linked_file" if has_pdf else "❌ 无附件（仅元数据）"
        col_info = f"\nCollection: {collection}" if collection else ""
        return [TextContent(type="text", text=f"✅ Zotero 导入成功\n- Key: {item_key}\n- 标题: {title[:60]}\n- 附件: {linked}{col_info}")]

    # ====================================================================
    # ingest_paper (REDO: + pdf_path + collection)
    # ====================================================================
    elif name == "ingest_paper":
        zotero_key = arguments.get("zotero_key")
        pdf_path_str = arguments.get("pdf_path")
        batch_collection = (arguments.get("batch_collection") or "").strip()
        collection = arguments.get("collection")
        force = arguments.get("force", False)

        # === Batch collection mode ===
        if batch_collection:
            from pathlib import Path as _Path

            def _batch_ingest():
                zot = zotero_sync._get_client()
                collections = zotero_sync.list_collections(zot)
                coll_key = None
                for c in collections:
                    if c["name"] == batch_collection:
                        coll_key = c["key"]
                        break
                if coll_key is None:
                    return None, batch_collection

                items = zotero_sync.list_items(zot, collection_key=coll_key, check_pdf=False)
                if not items:
                    return [], batch_collection

                stats = {"total": len(items), "success": 0, "skipped": 0, "no_pdf": 0, "chunks": 0}
                results = []
                for item in items:
                    key = item["key"]
                    title = item.get("title", "?")[:60]
                    result = _ingest_paper(item, force_parse=force, collection=collection)
                    if result["added"] > 0:
                        stats["success"] += 1
                        stats["chunks"] += result["added"]
                        results.append(f"  ✅ {key}: {title} ({result['added']} chunks)")
                    elif result["skipped"] > 0:
                        stats["skipped"] += 1
                        results.append(f"  ⚠️  {key}: {title} (部分跳过)")
                    else:
                        # Check if already ingested vs no PDF
                        existing = vector_store.get_chunks_by_key(key, collection_name=collection)
                        if existing:
                            stats["skipped"] += 1
                            results.append(f"  ⏭️  {key}: {title} (已入库)")
                        else:
                            stats["no_pdf"] += 1
                            results.append(f"  ⬜ {key}: {title} (无 PDF)")
                return stats, results

            batch_result = await _asyncio.to_thread(_batch_ingest)
            if batch_result[0] is None:
                return [TextContent(type="text", text=f"❌ 未找到 Zotero 文件夹: {batch_collection}")]
            if batch_result[0] == []:
                return [TextContent(type="text", text=f"Zotero 文件夹 '{batch_collection}' 中没有论文")]

            stats, results = batch_result
            col_info = f" → {collection}" if collection else ""
            msg = (
                f"## 批量入库: {batch_collection}{col_info}\n\n"
                f"总计 {stats['total']} 篇 | ✅ 入库 {stats['success']} | ⏭️ 跳过 {stats['skipped']} "
                f"| ⬜ 无PDF {stats['no_pdf']} | 📄 {stats['chunks']} chunks\n\n"
            )
            msg += "\n".join(results)
            if stats["no_pdf"] > 0:
                msg += "\n\n💡 无 PDF 的论文需手动下载 PDF 后单独调用 ingest_paper(zotero_key=...)。"
            return [TextContent(type="text", text=msg)]

        # === Single paper mode (zotero_key or pdf_path) ===
        if not zotero_key and not pdf_path_str:
            return [TextContent(type="text", text="需要提供 zotero_key、pdf_path 或 batch_collection 参数")]

        cross_warning = ""
        if zotero_key and pdf_path_str:
            cross_warning = (
                "⚠️ 同时提供了 zotero_key 和 pdf_path。"
                "将跳过 Zotero 下载，直接使用本地 PDF。"
                "请确保该 PDF 与此论文匹配。\n\n"
            )

        def _do_ingest():
            if zotero_key:
                _target = zotero_sync.get_item_by_key(zotero_key)
                if _target is None:
                    return None, {"added": 0, "skipped": 0, "skipped_details": []}, False
                return _target, _ingest_paper(_target, force_parse=force, pdf_path=pdf_path_str, collection=collection), True
            else:
                # From local PDF only: extract metadata → auto-import to Zotero → ingest
                from pathlib import Path as _Path
                _pdf = _Path(pdf_path_str)
                if not _pdf.exists():
                    return None, {"added": 0, "skipped": 0, "skipped_details": []}, False

                meta = pdf_parser.extract_pdf_metadata(_pdf)
                paper = {
                    "title": meta["title"],
                    "doi": "",
                    "authors": meta["authors"],
                    "year": meta["year"],
                    "abstract": "",
                    "url": "",
                    "open_access_pdf": None,
                    "source": "pdf_metadata",
                }

                zk = paper_importer.import_to_zotero(paper, _pdf, collection)
                if zk:
                    _target = {
                        "key": zk,
                        "title": meta["title"],
                        "authors": meta["authors"],
                        "year": meta["year"],
                        "doi": "",
                        "url": "",
                        "abstract": "",
                        "collection_names": [collection] if collection else [config.DEFAULT_COLLECTION],
                    }
                    _result = _ingest_paper(_target, force_parse=force, pdf_path=pdf_path_str, collection=collection)
                    return _target, _result, True  # extra flag: zotero_imported
                else:
                    logger.warning(f"Zotero import failed for {_pdf.name}, refusing to create orphan entries")
                    return None, None, False  # target=None, result=None = signal Zotero import failure

        target, result, zotero_imported = await _asyncio.to_thread(_do_ingest)

        if target is None:
            if zotero_key:
                return [TextContent(type="text", text=f"未找到论文 (key={zotero_key})")]
            if result is None:
                return [TextContent(type="text", text=(
                    f"❌ Zotero 自动导入失败，论文未入库 ChromaDB。\n"
                    f"PDF: {pdf_path_str}\n"
                    f"请先调用 import_to_zotero 手动导入，再用 zotero_key 调用 ingest_paper。"
                ))]
            return [TextContent(type="text", text=f"PDF 不存在: {pdf_path_str}")]

        col_info = f" → {collection}" if collection else ""

        if zotero_key:
            prefix = ""
        elif zotero_imported:
            prefix = f"✅ Zotero 导入: key={target['key']}, 提取标题: \"{target.get('title', '?')[:50]}\"\n"

        msg = f"{cross_warning}{prefix}✅ 入库完成: {target.get('title', '?')[:50]}\n添加 {result['added']} 个文本块{col_info}"
        if result["skipped"] > 0:
            msg += f"\n\n⚠️ 跳过 {result['skipped']} 个文本块（嵌入失败）:\n"
            for sd in result["skipped_details"]:
                col_label = f" [{sd.get('collection', '')}]" if sd.get('collection') else ""
                msg += f"  - [chunk {sd['chunk_index']}]{col_label} {sd['section'][:30]}: {sd['text_preview'][:60]}...\n"
            msg += "\n可能原因: 文本触发了智谱 Embedding API 内容安全过滤。这些段落将不参与语义搜索。"
        return [TextContent(type="text", text=msg)]

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
        name_map = config.get_name_map_snapshot()  # zh -> en mapping

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
            output.append(f"- **{f['name']}** (key={f['key']}, {f['item_count']}个条目) {sync_icon}")

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
            raw_results = await _asyncio.to_thread(
                vector_store.search,
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
            chroma_results = await _asyncio.to_thread(vector_store.search, identifier, n_results=1)
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
    try:
        async with stdio_server() as (read_stream, write_stream):
            logger.info("Zotero Brain MCP Server starting (Phase 4: 11 tools)")
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    except Exception as e:
        logger.critical(f"Server run failed: {type(e).__name__}: {e}", exc_info=True)
        raise
    finally:
        paper_discovery.close_client()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("MCP Server stopped by user")
    except Exception as e:
        logger.critical(f"MCP Server crashed: {type(e).__name__}: {e}", exc_info=True)
        sys.exit(1)
