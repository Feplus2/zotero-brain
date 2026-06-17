# -*- coding: utf-8 -*-
"""
Text Chunker - 将长文本切分为适合 Embedding 的小块

策略：
  1. 优先按 Markdown 标题（# ## ###）切分
  2. 每个 chunk 约 500-1000 字
  3. 保留元数据（论文标题、作者、章节）
"""

import re
from dataclasses import dataclass


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
    cn_pattern = re.compile(r"([。！？])")
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
    """Split by English sentence endings (.!?) followed by space and capital letter."""
    pattern = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')
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
