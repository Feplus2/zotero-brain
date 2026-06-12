# -*- coding: utf-8 -*-
"""
MCP Server - expose Zotero Brain to WorkBuddy.

Tools provided (10):
  - search_papers: semantic search in library (supports paper_keys filter)
  - compare_papers: compare multiple papers
  - get_bibtex: generate BibTeX citation
  - list_collections: list all Collections
  - ingest_paper: ingest a single paper
  - get_paper_chunks: list paper chunk structure
  - expand_context: context expansion around a chunk
  - read_paper_full: read full paper text
  - discover_papers: discover new papers from academic databases
  - fetch_and_ingest: download + import + ingest
"""

import logging
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

def _ingest_paper(item: dict, force_parse: bool = False, pdf_path: str | None = None) -> int:
    """Ingest a single paper, return chunk count.

    Args:
        pdf_path: known PDF path (skip Zotero download), passed by fetch_and_ingest
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
    target_collections = item.get("collection_names", [config.DEFAULT_COLLECTION])
    total = 0
    for col_name in target_collections:
        total += vector_store.add_chunks(chunks, collection_name=col_name)

    return total


# ============================================================================
# Tool definitions
# ============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools."""
    return [
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
                        "description": "指定搜索的 Collection 列表（如 ['钠电层状氧化物正极', '自动化实验室']），留空表示搜索全部",
                    },
                    "paper_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定在某篇或某几篇论文内搜索（Zotero key 列表）。用于精准讨论单篇论文时锁定检索范围。",
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
            name="compare_papers",
            description="对比多篇论文的方法、结论等（需要提供论文标题或 DOI）",
            inputSchema={
                "type": "object",
                "properties": {
                    "paper_identifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "论文标识列表（标题、DOI 或 Zotero key）",
                    },
                    "aspect": {
                        "type": "string",
                        "description": "对比维度（如 '方法', '结论', '实验设计'）",
                        "default": "综合对比",
                    },
                },
                "required": ["paper_identifiers"],
            },
        ),
        Tool(
            name="get_bibtex",
            description="生成论文的 BibTeX 引用格式",
            inputSchema={
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "论文标识（标题、DOI 或 Zotero key）",
                    },
                },
                "required": ["identifier"],
            },
        ),
        Tool(
            name="list_collections",
            description="列出文献库中所有 Collection 及其论文数量",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="ingest_paper",
            description="将单篇论文入库到知识库（需要提供 Zotero key）",
            inputSchema={
                "type": "object",
                "properties": {
                    "zotero_key": {
                        "type": "string",
                        "description": "Zotero 论文 key",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "是否强制重新解析",
                        "default": False,
                    },
                },
                "required": ["zotero_key"],
            },
        ),
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
                        "description": "数据源（默认全部）。OpenAlex 为主力（免费无 key），其余为 fallback。可选: openalex, arxiv, crossref, semantic_scholar",
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
        Tool(
            name="fetch_and_ingest",
            description="下载论文 PDF 并自动导入 Zotero + OCR + 向量化入库。6 级下载瀑布: 本地缓存 -> OpenAlex OA -> Unpaywall -> CORE -> arXiv -> Sci-Hub 镜像轮询。如果全部失败，仍创建 Zotero 条目并提供手动下载入口。",
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
                    "collection": {
                        "type": "string",
                        "description": "导入到的 Collection 中文名（如 '钠电层状氧化物正极'）。不指定则放入 uncategorized。",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "强制重新处理（即使已存在于库中）",
                        "default": False,
                    },
                },
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

    # search_papers
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

    # compare_papers
    elif name == "compare_papers":
        paper_ids = arguments["paper_identifiers"]
        aspect = arguments.get("aspect", "综合对比")

        all_content = []
        for pid in paper_ids:
            results = vector_store.search(pid, n_results=3)
            if results:
                meta = results[0]["metadata"]
                texts = [r["text"] for r in results]
                all_content.append({
                    "title": meta.get("title", pid),
                    "authors": meta.get("authors", "?"),
                    "year": meta.get("year", "?"),
                    "content": "\n".join(texts),
                })

        if not all_content:
            return [TextContent(type="text", text="未找到指定论文")]

        output = [f"## 论文对比：{aspect}\n"]
        for i, paper in enumerate(all_content, 1):
            output.append(f"### {i}. {paper['title']}")
            output.append(f"- 作者: {paper['authors']}")
            output.append(f"- 年份: {paper['year']}")
            output.append(f"\n**相关内容摘要:**\n{paper['content'][:800]}...\n")

        return [TextContent(type="text", text="\n".join(output))]

    # get_bibtex
    elif name == "get_bibtex":
        identifier = arguments["identifier"]

        results = vector_store.search(identifier, n_results=1)
        if not results:
            return [TextContent(type="text", text=f"未找到论文: {identifier}")]

        meta = results[0]["metadata"]
        title = meta.get("title", "Unknown")
        authors = meta.get("authors", "Unknown")
        year = meta.get("year", "")
        doi = meta.get("doi", "")

        key = f"{authors.split(',')[0].strip()}_{year}".lower().replace(" ", "_")
        bibtex = f"""@article{{{key},
  title={{{title}}},
  author={{{authors}}},
  year={{{year}}},
  doi={{{doi}}}
}}"""

        return [TextContent(type="text", text=f"```bibtex\n{bibtex}\n```")]

    # list_collections
    elif name == "list_collections":
        collections = vector_store.list_collections()

        if not collections:
            return [TextContent(type="text", text="文献库为空")]

        output = ["## 文献库 Collections\n"]
        for col in collections:
            output.append(f"- **{col['name']}**: {col['count']} 个文本块")

        return [TextContent(type="text", text="\n".join(output))]

    # ingest_paper
    elif name == "ingest_paper":
        zotero_key = arguments["zotero_key"]
        force = arguments.get("force", False)

        import asyncio as _asyncio

        def _do_ingest():
            _items = zotero_sync.list_items(check_pdf=False)
            _target = None
            for _item in _items:
                if _item["key"] == zotero_key:
                    _target = _item
                    break
            if _target is None:
                return None, 0
            return _target, _ingest_paper(_target, force_parse=force)

        target, added = await _asyncio.to_thread(_do_ingest)

        if target is None:
            return [TextContent(type="text", text=f"未找到论文 (key={zotero_key})")]

        return [TextContent(type="text", text=f"入库完成: {target['title'][:50]}\n添加 {added} 个文本块")]

    # get_paper_chunks
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

    # expand_context
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

    # read_paper_full
    elif name == "read_paper_full":
        paper_key = arguments["paper_key"]

        full_text = vector_store.get_full_text(paper_key)
        if full_text is None:
            return [TextContent(type="text", text=f"未找到论文 {paper_key} 的解析缓存。请确认该论文已入库（parsed/ 目录下应有 {paper_key}.md）。")]

        char_count = len(full_text)
        output = f"## 论文全文 (key={paper_key}, {char_count} 字)\n\n{full_text}"

        return [TextContent(type="text", text=output)]

    # discover_papers
    elif name == "discover_papers":
        query = arguments["query"]
        sources = arguments.get("sources")
        limit = arguments.get("limit", 10)

        import asyncio as _asyncio
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

    # fetch_and_ingest
    elif name == "fetch_and_ingest":
        doi = arguments.get("doi")
        title = arguments.get("title")
        collection = arguments.get("collection")
        force = arguments.get("force", False)

        paper = None

        if doi:
            # If DOI provided, construct paper object directly
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
            # Try to fetch metadata from CrossRef using DOI
            try:
                import httpx, asyncio as _asyncio
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
                        name = f"{family} {given}".strip()
                        if name:
                            authors.append(name)
                    paper["authors"] = authors
                    pub = data.get("published", {})
                    date_parts = pub.get("date-parts", [[None]])
                    if date_parts and date_parts[0]:
                        paper["year"] = date_parts[0][0]
            except Exception as e:
                logger.warning(f"CrossRef metadata fetch failed: {e}")

        elif title:
            # Only title provided, discover first to find best match
            import asyncio as _asyncio
            papers = await _asyncio.to_thread(paper_discovery.discover, title, limit=5)
            if papers:
                # Take the most relevant one
                paper = papers[0]
            else:
                return [TextContent(type="text", text=f"未找到匹配的论文: {title}")]

        else:
            return [TextContent(type="text", text="需要提供 doi 或 title 参数")]

        # Execute download + ingest (run in thread to avoid blocking the event loop)
        import asyncio
        result = await asyncio.to_thread(
            paper_importer.fetch_and_ingest, paper, collection_name=collection, force=force
        )

        return [TextContent(type="text", text=result["message"])]

    else:
        return [TextContent(type="text", text=f"未知工具: {name}")]


# ============================================================================
# Server startup
# ============================================================================

async def main():
    """Start MCP Server."""
    async with stdio_server() as (read_stream, write_stream):
        logger.info("Zotero Brain MCP Server starting")
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import asyncio
    asyncio.run(main())
