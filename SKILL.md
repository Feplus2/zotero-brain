---
name: zotero-brain
description: |
  Zotero Brain MCP 工具使用指南。用于管理学术文献库：搜索论文、下载 PDF、导入 Zotero、
  向量化入库、语义引用推荐。当用户需要查找/下载/入库/引用学术论文时触发此 Skill。
  触发词：找论文、下载论文、入库、Zotero、BibTeX、引用推荐、文献库
version: 2.1.0
tags: [academic, zotero, literature, mcp]
---

# Zotero Brain — 学术文献管理 MCP 工具

## 概述

Zotero Brain 是一个 MCP Server，提供 11 个工具用于学术文献全生命周期管理。
核心理念：**Zotero 是用户看到的唯一视图**，ChromaDB 是内部实现细节。

---

## 项目架构与文件布局

理解这套系统是正确使用工具的前提。所有文件都在本地，不存在需要远程下载的情况。

> 以下路径以 `config.py` 中的变量名标注。具体值因用户环境而异，请在 `config.py` 或 `.env` 中查看实际配置。

```
PROJECT_DIR/                       # 项目根目录
│
├── mcp_server.py                  # MCP Server 入口（11 个工具）
├── config.py                      # 路径和 API 配置
├── zotero_sync.py                 # Zotero Web API 交互
├── paper_discovery.py             # 论文发现（OpenAlex/arXiv/CrossRef/S2）
├── paper_importer.py              # 下载瀑布 + Zotero 导入
├── pdf_parser.py                  # MinerU Cloud API（PDF → Markdown）
├── chunker.py                     # 文本切块
├── embedder.py                    # 智谱 Embedding-3 向量化
├── vector_store.py                # ChromaDB 向量存储
├── network_helper.py              # TUN 绕过（MinerU 国内直连）
├── run_ingest.py                  # 批量入库脚本
├── ingest_resume.py               # 增量补全入库（已解析未入库的论文入 ChromaDB）
│
├── parsed/                        # ★ MinerU 解析缓存（核心！）
│   └── {KEY}/                     # 以 Zotero item key 命名
│       ├── {KEY}.md               # 结构化 Markdown 全文
│       ├── images/                # 从 PDF 提取的图片（hash 命名）
│       └── {KEY}.pdf              # 偶有一份 PDF 副本
│
├── data/
│   ├── chroma_db/                 # ChromaDB 向量数据库
│   ├── papers/                    # ★ PDF 永久存储（linked_file 指向这里）
│   ├── downloads/                 # download_paper 的临时下载目录
│   └── collection_map.json        # 中文名 → ChromaDB slug 映射
│
└── ZOTERO_LOCAL_STORAGE/          # Zotero 本地 storage（备用位置）
    └── {KEY}/                     # 部分旧论文的 PDF 可能在这里
```

### 关键路径速查

| 用途 | config 变量 | 默认位置（相对于 PROJECT_DIR） | 说明 |
|------|-------------|-------------------------------|------|
| 解析缓存 | `PARSED_DIR` | `parsed/` | MinerU 解析的 Markdown + 图片，**禁止删除** |
| 永久 PDF | `PAPERS_DIR` | `data/papers/` | linked_file 指向这里 |
| Zotero 本地 | `ZOTERO_LOCAL_STORAGE` | `~/Zotero/storage/` | 部分旧论文 PDF |
| 临时下载 | — | `data/downloads/` | download_paper 产物 |
| 向量数据库 | `CHROMA_DIR` | `data/chroma_db/` | ChromaDB 持久化 |

### 数据流

```
PDF 来源（下载 / 用户提供 / Zotero storage）
  ↓
import_to_zotero()  →  创建 Zotero 条目（如传了 pdf_path 则同时归档到 PAPERS_DIR）
  ↓
ingest_paper()      →  ★ 确保 PDF 归档到 PAPERS_DIR（自动移动 + 更新 linked_file）
                       → 读 PDF → MinerU 解析 → 缓存写入 parsed/{KEY}/{KEY}.md + images/
                       → 切块 → 智谱 Embedding → ChromaDB 入库
  ↓
后续所有阅读操作    →  直接读 parsed/{KEY}/{KEY}.md 缓存，不碰 PDF
```

> **注意：** `ingest_paper` 会自动将 PDF 归档到 PAPERS_DIR 并更新 Zotero linked_file。
> 即使 Agent 跳过了 `import_to_zotero`（直接从 download → ingest），PDF 也会被正确归档。

### 文件查找优先级

当 Agent 需要找到某篇论文的 PDF 或解析结果时，按以下顺序查找：

**找 PDF：**
1. `PAPERS_DIR` — 永久存储目录，linked_file 指向这里
2. `parsed/{KEY}/` — 有时解析时保留了一份 PDF 副本
3. `ZOTERO_LOCAL_STORAGE/{KEY}/` — Zotero 本地 storage（旧论文）
4. **到此为止** — 找不到说明论文未入库，需要走下载流程

**找解析结果（Markdown/图片）：**
1. `parsed/{KEY}/{KEY}.md` — 结构化全文
2. `parsed/{KEY}/images/` — 提取的图片
3. 如果 `parsed/{KEY}/` 不存在 → 这篇论文还没 ingest，需要先 `ingest_paper`

---

## MCP 工具清单（11 个）

### 搜索类
| 工具 | 用途 |
|------|------|
| `search_papers` | 语义搜索知识库（ChromaDB 向量搜索，支持 paper_keys 锁定特定论文） |
| `discover_papers` | 从学术数据库搜索真实论文（OpenAlex/arXiv/CrossRef/S2） |

### 下载 + 导入 + 入库（解耦三件套）
| 工具 | 用途 |
|------|------|
| `download_paper` | 纯下载 PDF（6 级瀑布），不碰 Zotero 不碰 ChromaDB |
| `import_to_zotero` | 纯 Zotero 操作（创建条目 + linked_file 附件） |
| `ingest_paper` | PDF → OCR → chunk → embed → ChromaDB |

### Collection 管理
| 工具 | 用途 |
|------|------|
| `list_collections` | 同时返回 Zotero 文件夹 + ChromaDB collection + 同步状态 |
| `create_collection` | 同时创建 Zotero 文件夹 + ChromaDB collection |

### 引用
| 工具 | 用途 |
|------|------|
| `get_bibtex` | 双模式：精确引用（`mode="exact"`）+ 语义推荐（`mode="recommend"`） |

### 深度阅读
| 工具 | 用途 |
|------|------|
| `get_paper_chunks` | 论文 chunk 目录（结构预览） |
| `expand_context` | 上下文扩展（围绕特定 chunk 的前后文） |
| `read_paper_full` | 读全文（从 parsed/ 缓存读，不重新解析 PDF） |

---

## 操作规范

### 1. 已入库论文不要重新下载

已入库论文的 PDF 100% 在本地（PAPERS_DIR 或 Zotero storage）。对已入库论文调用 `download_paper` 只会浪费时间，且 Science/Nature 等付费期刊的下载瀑布大概率失败。

**判断论文是否已入库：** 先用 `search_papers(query="论文标题")` 搜索。如果搜到了，说明已入库，直接用 `read_paper_full` 或 `get_paper_chunks` 阅读。

**`download_paper` 仅用于：** 全新论文（还没入 Zotero 的）。且 Science 等付费刊大概率瀑布全失败，用户会手动提供 PDF。

### 2. parsed/ 缓存不可删除

`parsed/` 目录下的文件是 MinerU 解析的缓存，是后续所有阅读操作的数据来源。删除意味着需要重新调 MinerU API 解析（耗时 + 花钱）。

- `read_paper_full(paper_key)` 就是从 `parsed/{KEY}/` 读缓存
- `get_paper_chunks(paper_key)` 也是从 `parsed/{KEY}/` 读
- 如果 `read_paper_full` 报"未找到缓存"→ 这篇论文还没 ingest → 先 `ingest_paper`

### 3. parsed/ 不在 data/ 下

解析缓存在项目根目录下的 `parsed/`（即 `PARSED_DIR`），不在 `data/` 下。`data/` 下面是 `chroma_db/`、`papers/`、`downloads/`。不要混淆。

### 4. 图片来自 parsed/{KEY}/images/

MinerU 解析时从 PDF 中提取所有图片，保存在 `parsed/{KEY}/images/`。做 PPT、报告、论文解读时可以直接用这些图片。

图片文件名是 hash（如 `4bd60dfc6a...jpg`），需要从 `{KEY}.md` 中的 `![](images/xxx.jpg)` 引用关系来对应图片内容。

### 5. Collection 参数统一用中文名

所有工具的 `collection` 参数都接受中文文件夹名（如"钠电层状氧化物正极"），内部自动查 `collection_map.json` 映射到 ChromaDB slug。

### 6. 操作前先 list_collections

每次操作前先看文件夹和同步状态，了解当前文献库全貌，再决定下一步。

---

## 场景速查

### 场景 A：找论文并入库（最常见）

```
1. list_collections() → 看用户有哪些 Zotero 文件夹 + 同步状态
2. discover_papers(query="...") → 搜论文，返回候选列表
3. Agent 判断论文属于哪个文件夹
   → 匹配上了 → 从候选列表中提取 DOI，用 download_paper(doi="...")
             → 将返回的 metadata 传给 import_to_zotero(title=..., doi=..., pdf_path=...)
             → ingest_paper(zotero_key=...)
   → 没匹配上 → 问用户："这篇放哪个文件夹？还是新建一个？"
   → 用户想新建 → create_collection(folder_name="新名字", chroma_name="english-slug")
4. search_papers(query="论文标题") → 验证入库成功
```

### 场景 B：本地 PDF 入库

```
1. list_collections() → 看有哪些文件夹
2. Agent 判断放哪个文件夹（不确定就问用户）
3. import_to_zotero(title="...", pdf_path="/path/to/paper.pdf", collection="目标文件夹")
4. ingest_paper(pdf_path="/path/to/paper.pdf", collection="目标文件夹")
```

### 场景 B2：从 Zotero 文件夹批量入库

```
1. list_collections() → 确认目标文件夹存在且有 PDF
2. ingest_paper(batch_collection="固态电解质", collection="固态电解质")
   → 自动跳过已入库论文，只处理新增
   → 输出汇总：总计 N 篇 / 成功 M / 跳过 K / 无PDF P
```

> **注意：** `batch_collection` 和 `collection` 参数应保持一致，否则第二次调用时跳过检查可能失效导致重复解析。建议同时传两个参数且填相同值。

### 场景 C：完整下载 + 导入 + 入库

```
1. download_paper(doi="10.1038/xxx") → 返回 pdf_path + metadata
2. import_to_zotero(title=..., doi=..., pdf_path=pdf_path, collection="目标文件夹") → 返回 item_key
3. ingest_paper(zotero_key=item_key, collection="目标文件夹") → 入库完成
```

### 场景 D：阅读已入库论文

已入库论文的所有数据都在本地，直接读即可。

```
1. search_papers(query="关键词") → 找到相关论文和 chunk
2. get_paper_chunks(paper_key="KEY") → 看论文结构目录
3. expand_context(paper_key="KEY", chunk_index=N, prev=2, next=3) → 精读特定段落
4. read_paper_full(paper_key="KEY") → 需要全文时（token 消耗大，慎用）
```

### 场景 E：拿论文配图做 PPT / 报告

```
1. read_paper_full(paper_key="KEY") → 读 Markdown 全文
2. 从 Markdown 中找到 ![](images/xxx.jpg) 引用 → 确定需要的图片 hash
3. 图片实际路径：PARSED_DIR/{KEY}/images/{hash}.jpg
4. 复制图片到输出目录使用
```

### 场景 F：语义引用推荐（辅助写作）

```
1. get_bibtex(query="关于 LLZO 电解质界面稳定性的讨论", mode="recommend", n_results=5)
   → 返回 5 篇最相关论文 + 每篇的 BibTeX
2. Agent 根据上下文选择引用哪些
3. 对需要深入阅读的论文：
   a. search_papers(query="具体子问题", paper_keys=["推荐论文的 key"])
   b. expand_context(paper_key="key", chunk_index=N, prev=2, next=2)
   c. 特别重要的论文：read_paper_full(paper_key="key")
4. 综合理解后撰写段落，在适当位置插入 BibTeX 引用
```

### 场景 G：精确生成 BibTeX

```
1. get_bibtex(identifier="10.1038/nature12373", mode="exact")
   → 返回单篇 BibTeX（Zotero 优先 → ChromaDB fallback → CrossRef fallback）
```

---

## PDF 生命周期

```
download_paper(doi) → data/downloads/ 临时文件
    ↓
import_to_zotero() → 创建 Zotero 条目（如传了 pdf_path 则归档到 PAPERS_DIR）
    ↓
ingest_paper() → ★ 自动归档 PDF 到 PAPERS_DIR + 更新 Zotero linked_file
    → 从 PAPERS_DIR 读 PDF（不复制），MinerU 解析
    → 缓存写入 parsed/{KEY}/{KEY}.md + images/（只缓存文本，不缓存 PDF）
    ↓
后续所有操作 → 从 parsed/{KEY}/{KEY}.md 读缓存，不碰 PDF
```

**原则：整个流程 PDF 只存 1 份（在 PAPERS_DIR）。`ingest_paper` 是归档的最终保障。**

---

## Collection 命名规范

ChromaDB 只接受 `[a-z0-9._-]`（3-512 字符，首尾必须 a-z0-9）。

**创建新 Collection 时，Agent 负责将中文名翻译为英文 slug：**

| 中文名 | 英文 slug |
|--------|-----------|
| 钠电层状氧化物正极 | `sodium-layered-oxide-cathode` |
| 固态电解质 | `solid-electrolyte` |
| 钠离子电池 | `sodium-ion-battery` |
| 火焰喷雾热解 | `flame-spray-pyrolysis` |
| 电化学性能 | `electrochemical-performance` |

**命名规则：**
- 全小写，单词用 `-` 连接
- 使用学术领域标准英文术语
- 简洁但可辨识（不超过 50 字符）

---

## 注意事项

1. **download → import → ingest 是解耦的** — 可以只下载不导入，或只导入不入库
2. **get_bibtex 双模式** — `mode="exact"` 用于已知论文的精确引用，`mode="recommend"` 用于写作时的语义推荐
3. **Collection 映射** — 中英文映射由 `create_collection` 工具写入 `collection_map.json`
4. **网络要求** — MinerU（国内）必须直连不走代理；OpenAlex/Unpaywall/CrossRef/Zotero（境外）需要代理。`network_helper.py` 会自动处理 MinerU 的直连绕过
5. **download_paper 优先用 DOI** — `discover_papers` 返回的每条候选都有 DOI。调用 `download_paper` 时一律用 `doi` 参数定位论文，禁止仅用 `title` 搜索后盲取首个结果。`title` 参数仅作为元数据标签辅助使用。
6. **read_paper_full 只在用户明确要求时使用** — 平时优先使用 `get_paper_chunks` + `expand_context` 定位需要的段落，但在用户指定读全文时必须读完论文全部内容。
7. **PDF 可能货不对板** — 即使 DOI 正确，部分数据源（如 CORE、Sci-Hub）偶尔返回错误论文的 PDF。`download_paper` 已内置元数据标题校验，不匹配时会自动丢弃并尝试下一个源。如果所有源均因校验失败或下载失败而耗尽，告知用户并建议手动下载。导入和入库前请再次确认 PDF 内容与预期论文一致。
