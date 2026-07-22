# -*- coding: utf-8 -*-
"""
Text Chunker - 将论文内容切分为适合 Embedding 的小块

两条路径：
  1. chunk_content_list — 优先：基于 MinerU content_list 块级结构。
     标题层级只信论文自己的编号（1 / 1.1 / 1.1.1），不信 MinerU text_level；
     图/表成为独立 chunk（caption+描述入库，绝不因过短被丢弃）；
     metadata 带 section_path / page_start / page_end / chunk_type。
  2. chunk_markdown — 兜底：无 content_list 缓存时按 Markdown 标题切分。
入口统一走 chunk_auto。
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Chunk:
    """文本块"""
    text: str
    metadata: dict
    
    def __str__(self):
        return f"[{self.metadata.get('title', '?')}] {self.text[:100]}..."


def chunk_markdown(
    markdown_text: str,
    paper_metadata: dict | None = None,
    min_chunk_size: int = 200,
    max_chunk_size: int = 1500,
) -> list[Chunk]:
    """
    将 Markdown 文本切分为 Chunk 列表
    
    Args:
        markdown_text: MinerU 解析出的 Markdown 文本
        paper_metadata: 论文元数据（标题、作者、年份等）
        min_chunk_size: 最小 chunk 字数
        max_chunk_size: 最大 chunk 字数
    
    返回: [Chunk, ...]
    """
    if paper_metadata is None:
        paper_metadata = {}
    
    # Split by Markdown headings
    sections = _split_by_headings(markdown_text)
    
    chunks = []
    chunk_counter = 0  # Global counter, ensuring unique chunk_index per paper
    for section_title, section_content in sections:
        # If section is too long, split further
        if len(section_content) > max_chunk_size:
            sub_chunks = _split_long_text(section_content, max_chunk_size)
            for sub_text in sub_chunks:
                metadata = {
                    **paper_metadata,
                    "section": section_title,
                    "chunk_index": chunk_counter,
                }
                chunks.append(Chunk(text=sub_text.strip(), metadata=metadata))
                chunk_counter += 1
        else:
            metadata = {
                **paper_metadata,
                "section": section_title,
                "chunk_index": chunk_counter,
            }
            chunks.append(Chunk(text=section_content.strip(), metadata=metadata))
            chunk_counter += 1
    
    # Filter out chunks that are too short
    chunks = [c for c in chunks if len(c.text) >= min_chunk_size]

    return chunks


# ============================================================================
# content_list 块级切块（优先路径）
# ============================================================================

# MinerU content_list 中的页面家具类型，直接丢弃
_SKIP_BLOCK_TYPES = {"header", "footer", "page_number"}

# 论文编号标题：1 / 1.1 / 1.1.1（层级 = 编号深度，不信 MinerU text_level）
_HEADING_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+")


def chunk_auto(
    markdown_text: str,
    content_list: list[dict] | None = None,
    paper_metadata: dict | None = None,
    min_chunk_size: int = 200,
    max_chunk_size: int = 1500,
) -> list[Chunk]:
    """
    优先用 MinerU content_list（块级结构）切块；缺失或产出为空时回退 Markdown 切块。
    """
    if content_list:
        chunks = chunk_content_list(
            content_list,
            paper_metadata=paper_metadata,
            min_chunk_size=min_chunk_size,
            max_chunk_size=max_chunk_size,
        )
        if chunks:
            return chunks
        logger.warning("content_list chunking yielded nothing, fallback to markdown")
    return chunk_markdown(markdown_text, paper_metadata, min_chunk_size, max_chunk_size)


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _heading_level(title: str) -> int:
    """标题层级只信论文自己的编号（1 → 1 级，1.1 → 2 级，1.1.1 → 3 级）。
    无编号 → 1（顶层）。MinerU 的 text_level 不可靠（可能出现 ## 1.1.1）。"""
    m = _HEADING_NUM_RE.match(title)
    if not m:
        return 1
    return m.group(1).count(".") + 1


def _update_section_path(path: list[str], level: int, title: str) -> list[str]:
    """大纲栈：深一级压栈（允许跳级）、同级替换、浅级弹栈到目标层再压。"""
    level = max(1, level)
    if level > len(path):
        return path + [title]
    return path[: level - 1] + [title]


def _figure_chunk_text(block: dict) -> str:
    """图 chunk 文本：图片文件标记 + caption + VLM 描述 + footnote。"""
    parts = []
    img = (block.get("img_path") or "").rsplit("/", 1)[-1]
    if img:
        parts.append(f"[图 {img}]")
    parts.extend(c.strip() for c in (block.get("image_caption") or []) if _norm_text(c))
    if _norm_text(block.get("content", "")):
        parts.append(block["content"].strip())
    parts.extend(f.strip() for f in (block.get("image_footnote") or []) if _norm_text(f))
    return "\n".join(parts)


def _table_chunk_text(block: dict) -> str:
    """表 chunk 文本：caption + table_body + footnote。"""
    parts = ["[表]"]
    parts.extend(c.strip() for c in (block.get("table_caption") or []) if _norm_text(c))
    body = (block.get("table_body") or "").strip()
    if body:
        parts.append(body)
    parts.extend(f.strip() for f in (block.get("table_footnote") or []) if _norm_text(f))
    return "\n".join(parts) if len(parts) > 1 else ""


def _chart_chunk_text(block: dict) -> str:
    """chart（曲线图/统计图）chunk 文本：图片文件标记 + caption + VLM 描述 + footnote。"""
    parts = []
    img = (block.get("img_path") or "").rsplit("/", 1)[-1]
    if img:
        parts.append(f"[图 {img}]")
    parts.extend(c.strip() for c in (block.get("chart_caption") or []) if _norm_text(c))
    if _norm_text(block.get("content", "")):
        parts.append(block["content"].strip())
    parts.extend(f.strip() for f in (block.get("chart_footnote") or []) if _norm_text(f))
    return "\n".join(parts)


def chunk_content_list(
    blocks: list[dict],
    paper_metadata: dict | None = None,
    min_chunk_size: int = 200,
    max_chunk_size: int = 1500,
) -> list[Chunk]:
    """
    用 MinerU content_list（v1 扁平块序列）切块。

    与 Markdown 路径的差异：
      - 标题层级来自论文编号（_heading_level），不信 text_level
      - 图/表是独立 chunk（不受 min_chunk_size 限制，绝不丢弃）
      - 与图/表 caption 完全重复的文本块不再入库（去重）
      - metadata 增加 section_path / chunk_type / page_start / page_end / image
    """
    if paper_metadata is None:
        paper_metadata = {}

    # caption 去重集合：正文中与图/表 caption 完全相同的文本块不再重复入库
    caption_texts = set()
    for b in blocks:
        if b.get("type") == "image":
            caption_texts.update(_norm_text(c) for c in (b.get("image_caption") or []))
        elif b.get("type") == "table":
            caption_texts.update(_norm_text(c) for c in (b.get("table_caption") or []))
        elif b.get("type") == "chart":
            caption_texts.update(_norm_text(c) for c in (b.get("chart_caption") or []))
    caption_texts.discard("")

    chunks: list[Chunk] = []
    section_path: list[str] = []
    acc: list[str] = []        # 累积正文单元（段落/公式）
    acc_pages: list[int] = []
    chunk_counter = 0

    def section_meta() -> dict:
        return {
            "section": section_path[-1] if section_path else "",
            "section_path": " > ".join(section_path),
        }

    def flush_text():
        nonlocal chunk_counter, acc, acc_pages
        text = "\n\n".join(t for t in acc if t.strip()).strip()
        pages = acc_pages
        acc, acc_pages = [], []
        if not text:
            return
        pieces = [text] if len(text) <= max_chunk_size else _split_long_text(text, max_chunk_size)
        for piece in pieces:
            piece = piece.strip()
            if len(piece) < min_chunk_size:
                continue
            chunks.append(Chunk(text=piece, metadata={
                **paper_metadata,
                **section_meta(),
                "chunk_index": chunk_counter,
                "chunk_type": "text",
                "page_start": min(pages) if pages else 0,
                "page_end": max(pages) if pages else 0,
            }))
            chunk_counter += 1

    for b in blocks:
        btype = b.get("type")
        if btype in _SKIP_BLOCK_TYPES:
            continue
        page = b.get("page_idx", 0)

        # 标题块：更新大纲栈，不进入正文流
        if btype == "text" and b.get("text_level"):
            title = _norm_text(b.get("text", ""))
            if title:
                flush_text()
                section_path = _update_section_path(section_path, _heading_level(title), title)
            continue

        # 图/表/chart：独立 chunk，先冲刷累积正文
        if btype in ("image", "table", "chart"):
            flush_text()
            if btype == "image":
                text, ctype = _figure_chunk_text(b), "figure"
            elif btype == "table":
                text, ctype = _table_chunk_text(b), "table"
            else:
                text, ctype = _chart_chunk_text(b), "chart"
            if text:
                meta = {
                    **paper_metadata,
                    **section_meta(),
                    "chunk_index": chunk_counter,
                    "chunk_type": ctype,
                    "page_start": page,
                    "page_end": page,
                }
                if btype in ("image", "chart"):
                    meta["image"] = (b.get("img_path") or "").rsplit("/", 1)[-1]
                chunks.append(Chunk(text=text, metadata=meta))
                chunk_counter += 1
            continue

        # 行间公式：作为文本单元进入正文流
        if btype in ("equation", "interline_equation"):
            tex = (b.get("text") or "").strip()
            if tex:
                acc.append(f"$${tex}$$")
                acc_pages.append(page)
            continue

        # 普通文本块
        text = _norm_text(b.get("text", ""))
        if not text or text in caption_texts:
            continue
        acc.append(text)
        acc_pages.append(page)
        if sum(len(t) for t in acc) >= max_chunk_size:
            flush_text()

    flush_text()
    return chunks


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """
    按 Markdown 标题切分文本
    
    返回: [(section_title, section_content), ...]
    """
    # Match lines starting with #, ##, or ###
    heading_pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    
    sections = []
    last_pos = 0
    last_title = ""
    
    for match in heading_pattern.finditer(text):
        # Save the previous section
        if last_pos < match.start():
            sections.append((last_title, text[last_pos:match.start()]))
        
        # Update current position
        last_pos = match.end()
        last_title = match.group(2).strip()
    
    # Last section
    if last_pos < len(text):
        sections.append((last_title, text[last_pos:]))
    
    return sections


def _split_long_text(text: str, max_size: int) -> list[str]:
    """
    将长文本按段落切分，每段不超过 max_size
    
    策略：
      1. 优先按段落（\n\n）切分
      2. 如果段落还是太长，按句子切分
    """
    paragraphs = text.split("\n\n")
    
    chunks = []
    current_chunk = []
    current_size = 0
    
    for para in paragraphs:
        para_size = len(para)
        
        # If current paragraph exceeds max_size, split by sentences
        if para_size > max_size:
            # Save existing chunks first
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_size = 0
            
            # Split paragraph by sentences
            sentences = _split_into_sentences(para)
            for sent in sentences:
                if len(sent) > max_size:
                    sent = sent[:max_size - 3] + "..."
                if current_size + len(sent) > max_size and current_chunk:
                    chunks.append("\n\n".join(current_chunk))
                    current_chunk = []
                    current_size = 0
                current_chunk.append(sent)
                current_size += len(sent)
        
        # Normal paragraph
        elif current_size + para_size > max_size and current_chunk:
            # Exceeds limit, save current chunk and start a new one
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [para]
            current_size = para_size
        else:
            current_chunk.append(para)
            current_size += para_size
    
    # Last paragraph
    if current_chunk:
        chunks.append("\n\n".join(current_chunk))
    
    return chunks


def _split_into_sentences(text: str) -> list[str]:
    """按句子切分：中文优先，无中文边界则回退英文断句。"""
    cn_pattern = re.compile(r"([。！？；])")
    parts = cn_pattern.split(text)

    sentences = []
    for i in range(0, len(parts) - 1, 2):
        sent = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if sent.strip():
            sentences.append(sent)
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append(parts[-1])

    if len(sentences) <= 1:
        sentences = _split_english_sentences(text)

    return sentences


def _split_english_sentences(text: str) -> list[str]:
    """Split by English sentence endings (.!?) followed by whitespace.

    放宽到任意字母/数字开头（不再要求大写）：过度切分无害（句子会被重新装箱），
    但能避免 "et al. showed" 粘成超长句后触发硬截断。"""
    pattern = re.compile(r'(?<=[.!?])\s+(?=[A-Za-z0-9("(])')
    raw = pattern.split(text)
    return [s.strip() for s in raw if s.strip()]


if __name__ == "__main__":
    # Test
    sample_text = """
# 第一章 引言

固态电池是一种新型电池技术，使用固态电解质代替传统液态电解质。

## 1.1 研究背景

传统锂离子电池存在安全隐患，液态电解质易燃易爆。固态电解质可以解决这个问题。

## 1.2 研究意义

固态电池具有更高的能量密度和安全性，是下一代电池技术的重要方向。

# 第二章 方法

本章介绍实验方法。

## 2.1 材料制备

使用 LLZO 作为固态电解质材料。
"""
    
    chunks = chunk_markdown(
        sample_text,
        paper_metadata={"title": "固态电池研究", "authors": ["Wang"]},
    )
    
    print(f"切分为 {len(chunks)} 个 chunk:\n")
    for i, chunk in enumerate(chunks, 1):
        print(f"Chunk {i}:")
        print(f"  元数据: {chunk.metadata}")
        print(f"  内容: {chunk.text[:100]}...")
        print()
