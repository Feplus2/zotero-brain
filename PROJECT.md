# Zotero Brain

> 将 Zotero 变成"活的知识库"——RAG 管线 + MCP 服务，让 AI Agent 能语义检索、对话阅读、对比分析你的文献库。

---

## 项目目标

Zotero 管理文献很强大，但它本质是一个"死"的数据库——只能按标题/作者/标签搜索，无法按**语义**搜索，更不能与文献"对话"。

**Zotero Brain** 的目标是：把 Zotero 里的每一篇论文 PDF，通过 MinerU 解析为结构化 Markdown → 切块 → 智谱 Embedding-3 向量化 → 存入 ChromaDB，最后通过 MCP Server 暴露给 AI Agent（如 WorkBuddy / Trae），实现：

- 语义搜索（"帮我找固态电解质界面稳定性相关论文"→ 精准命中）
- 多篇论文横向对比（方法、结论、实验设计）
- 基于全文内容的问答（不依赖摘要，不靠 LLM 编造）
- BibTeX 引用导出（直接粘进 LaTeX）
- 按 Zotero Collection 自动分库（电池归电池，生物归生物，互不干扰）

---

## 三阶段路线图

```
Phase 1 ─── 最小可用版 ─── ✅ 已完成 (2026-06-09)
Phase 1.5 ── 渐进式检索 ── ✅ 已完成 (2026-06-10)
Phase 2 ─── 报告生成   ─── ⏳ 待开工
Phase 2.5 ── 论文发现与自动入库 ── ✅ 已完成 (2026-06-12)
Phase 3 ─── 自动化     ─── 📅 后续
Release ── 代码审查收尾 ── ✅ 已完成 (2026-06-12)
```

---

### Phase 1：最小可用版 ✅ 已完成

**目标**：搭通整条管线，实现语义搜索 + 问答 + BibTeX 导出。

```
Zotero → pyzotero 拉文献元数据+附件
    ↓
MinerU VLM → PDF 解析成结构化 Markdown
    ↓
文本分块 → 智谱 Embedding-3 → ChromaDB（按 Collection 分库）
    ↓
MCP Server → 暴露给 AI Agent
```

**Phase 1 能力清单：**

| 你能做什么                                | 状态 |
| ----------------------------------------- | ---- |
| "我文献库里有哪些关于 LLZO 的论文？"      | ✅    |
| 多篇论文横向对比（方法、结论、实验设计）  | ✅    |
| BibTeX 导出（直接粘进 LaTeX）             | ✅    |
| 列出所有 Collection                       | ✅    |
| 单篇论文入库（手动触发）                  | ✅    |

**Phase 1 进度表：**

| 项目             | 状态                                    |
| ---------------- | --------------------------------------- |
| Zotero 连接      | 129 篇论文，6 个 Collection              |
| PDF 解析         | 125 篇已用 MinerU VLM 解析为 Markdown    |
| 向量入库         | 125 篇已入库 ChromaDB（8082+ chunks）    |
| MCP Server       | 10 个工具（5 个 Phase 1 + 3 个 Phase 1.5 + 2 个 Phase 2.5） |
| WorkBuddy 集成   | ✅ 已配置 `mcp.json`，MCP Server 正常连接  |
| 渐进式检索       | ✅ 3 个新工具（借鉴 SageRead RAG 策略）    |

---

### Phase 1.5：渐进式检索 ✅ 已完成 (2026-06-10)

**灵感来源**：SageRead（epub 阅读工具）的渐进式 RAG 策略——先粗搜定位 → 扩展上下文 → 必要时全文灌入。

**新增 3 个 MCP 工具：**

| 工具名              | 用途                                                   |
| ------------------- | ------------------------------------------------------ |
| `get_paper_chunks`  | 列出某篇论文的 chunk 目录（编号+章节+摘要），了解结构  |
| `expand_context`    | 围绕某个 chunk 扩展上下文（前N后N个chunk完整文本）     |
| `read_paper_full`   | 读取单篇论文全文（从 parsed/ 缓存，不重新解析 PDF）    |

**增强 1 个工具：**

- `search_papers` 新增 `paper_keys` 参数——支持锁定在单篇/多篇论文内搜索

**使用流程：**
```
Step 1: search_papers("mitigation strategies", paper_keys=["B9NK7L9S"])
        → 定位到 chunk 23（相似度最高）

Step 2: expand_context(paper_key="B9NK7L9S", chunk_index=23, prev=1, next=2)
        → 拿到 chunk 21-25 的完整文本，上下文连贯

Step 3（如需精准）: read_paper_full(paper_key="B9NK7L9S")
        → 18万字全文直接进 LLM 上下文（DeepSeek V4 Pro，几毛钱搞定）
```

**MCP 配置：**
- WorkBuddy MCP 配置路径：`C:\Users\20995\.workbuddy\mcp.json`
- zotero-brain 已配置并验证通过

---

### Phase 2.5：论文发现与自动入库 ✅ 已完成 (2026-06-12)

**目标**：实现从"发现论文"到"入库可查"的全自动一条龙，搜索层多源冗余，下载层 6 级瀑布。

**管线设计：**
```
用户对话 / 关键词 / DOI
    ↓
OpenAlex (主力) / CrossRef / arXiv / Semantic Scholar (fallback) → 检索论文
    ↓
去重检测（ChromaDB metadata 精确匹配 + Zotero 标题搜索+DOI 过滤）
    ↓ 未命中
下载瀑布: OpenAlex oa_url → Unpaywall → CORE → arXiv → Sci-Hub (镜像轮询) → 手动
    ↓
导入 Zotero（pyzotero create item + linked_file 链接附件，PDF 不上传云端）
    ↓
MinerU OCR/VLM 解析 → 结构化 Markdown
    ↓
切块 → 智谱 Embedding-3 → ChromaDB 向量化
```

**已完成的部分：**
- ✅ `paper_discovery.py` — 4 源搜索框架（OpenAlex/CrossRef/arXiv/S2）+ `is_in_library()` ChromaDB metadata where 过滤
- ✅ `paper_importer.py` — 瀑布式下载 + 本地 PDF 缓存 + pyzotero 创建条目（linked_file 附件）+ 双重去重 + 自动触发 ingest
- ✅ `vector_store.py` — `exists_by_metadata()` 精确查重函数
- ✅ MCP 工具 `discover_papers` 和 `fetch_and_ingest`（10 个工具全部在线）
- ✅ `network_helper.py` — MinerU TUN 绕过（DoH 延迟加载 + httpx 猴子补丁 + CDN 子域名独立解析）
- ✅ 端到端去重验证通过（已入库论文正确返回 `status: skipped`）
- ✅ 新论文全流程验证通过（arXiv 预印本 BBZ86VJF: 下载→Zotero→MinerU 80K 字符→54 chunks→ChromaDB）
- ✅ `config.py` 新增 `OPENALEX_EMAIL`、`CORE_API_KEY` 配置项

**✅ 本阶段全部完成 (2026-06-12)：**

1. **`paper_discovery.py` — `_search_openalex()` 函数** ✅
   - API: `https://api.openalex.org/works?search={query}&per_page={limit}&mailto={OPENALEX_EMAIL}`
   - 提取: title, authorships→authors, publication_year, doi, abstract_inverted_index→plain text, cited_by_count, open_access.oa_url
   - `abstract_inverted_index` 倒排索引格式 `{word: [positions]}` 已实现 `_reconstruct_abstract()` 重建成原文
   - 已注册到 `source_map` 和 `discover()` 默认 sources 列表（放第一位）
   - MCP `discover_papers` sources enum 已包含 `"openalex"`

2. **`paper_importer.py` — 下载瀑布升级为 6 级** ✅
   - `_download_openalex(oa_url)` — 直接下载 OA PDF（瀑布第 2 位，仅次于本地缓存）
   - `_download_core(doi)` — CORE API `https://api.core.ac.uk/v3/search/works?q=doi:{doi}`，提取 downloadUrl
   - Sci-Hub 改为镜像轮询：`_get_scihub_mirrors()` 从 `whereisscihub-rs28c.ondigitalocean.app` 获取活跃镜像，缓存 1 小时，逐个尝试
   - `download_pdf()` 瀑布顺序: cache → OpenAlex oa_url → Unpaywall → CORE → arXiv → Sci-Hub mirrors → fail
   - 失败时返回 DOI 链接 + Sci-Hub 入口 + 论文页面 URL 提示用户手动下载

3. **配置项** ✅
   - `config.py` 已有 `OPENALEX_EMAIL`（默认复用 UNPAYWALL_EMAIL）和 `CORE_API_KEY`
   - 用户可选申请 CORE API key: https://core.ac.uk → API Keys → 免费（每天 5000 次请求），写入 `.env`

**Phase 2.5 能力清单：**

| 你能做什么                                              | MCP 工具               | 状态 |
| ------------------------------------------------------- | ---------------------- | ---- |
| "帮我搜一下最近关于 LLZO 的论文" → 返回候选列表         | `discover_papers`      | ✅    |
| "把这篇论文下载并入库" → 全自动一条龙 + 自动去重        | `fetch_and_ingest`     | ✅    |
| "我关注的这几个方向有没有新论文" → 定期检查             | 自动化定时任务          | ⏳    |

**实现细节：**
- `paper_discovery.py`：使用原始 httpx；`is_in_library()` 为 ChromaDB metadata `where` 精确匹配（DOI + title）；docstring 已更新为 OpenAlex 主力
- `paper_importer.py`：瀑布式下载（当前: cache → Unpaywall → arXiv → Sci-Hub）→ pyzotero 创建条目（`template["collections"]` + `linked_file`）→ 触发 ingest
- `paper_importer.py` 去重：① ChromaDB `exists_by_metadata("doi", doi)` ② Zotero `zot.items(q=title_words)` + DOI 精确过滤
- `network_helper.py`：应用层猴子补丁 httpx.Client._send_single_request，MinerU 直连（DoH 延迟加载，首次请求时才解析 IP），CDN 子域名独立解析
- `mcp_server.py` 启动时自动 `network_helper.install()`（0ms 完成，DoH 延迟到首次 MinerU 请求时执行）
- `config.py`：`OPENALEX_EMAIL`（用于 OpenAlex polite pool 加速）、`CORE_API_KEY`

**API 踩坑记录：**
- Semantic Scholar DOI 在 `externalIds['DOI']`，不是 `.doi` 属性
- arXiv API 必须 `https://` 不是 `http://`
- Unpaywall 需要合法 email（`test@example.com` 被 422）
- `semanticscholar` Python 库 Windows 上崩溃 → 改用原始 httpx
- Semantic Scholar 无 key 时严格限流 → 已弃用为主力，降级为 fallback
- Zotero API 的 `q` 参数不索引 DOI 字段 → 必须用标题关键词搜索 + 结果 DOI 过滤
- ChromaDB 语义搜索不适合精确去重（DOI 跟段落文本余弦相似度极低）→ 改用 metadata `where` 过滤
- pyzotero `delete_item()` 有 bug（TypeError）→ 用 raw HTTP API 删除
- SSRN PDF 被 Cloudflare 拦截（需 JS challenge），程序化下载不可行 → 需用户手动下载
- OpenAlex `abstract_inverted_index` 是倒排索引格式，需重建成原文: `{word: [pos1, pos2]}` → 按 position 排列拼接
- Sci-Hub 镜像不稳定，存在永久入口页: `whereisscihub-rs28c.ondigitalocean.app`（返回活跃镜像列表）

**预估工作量**：搜索层 OpenAlex 函数 + 下载瀑布升级，约 1 个会话

---

### Phase 4：工具解耦 + Zotero-First 设计 ✅ 完成

**核心原则：**

1. **Zotero 是唯一真实视图** — 用户只看到 Zotero 文件夹，不知道 ChromaDB 的存在
2. **MCP 只做执行，Agent 做决策** — 分类、翻译、是否创建新文件夹都交给 Agent + Skill
3. **砍掉 DeepSeek API** — Collection 命名由 Agent 翻译成英文 slug，不需要额外 API
4. **工具职责单一** — 每个工具只做一件事，流程编排交给 Skill

**Git 存档点：** `3c6050a` (2026-06-12) — Phase 1/1.5/2.5 完成后的初始 commit，Phase 4 改动前的安全快照。

---

#### 4.1 工具变更总览

**当前 10 个工具 → Phase 4 后 11 个工具：**

| # | 工具 | 变更 | 涉及文件 |
|---|------|------|----------|
| 1 | `download_paper` | 🆕 新增 | `paper_importer.py`（复用现有 `download_pdf()`） |
| 2 | `import_to_zotero` | 🆕 新增 | `paper_importer.py`（复用现有 `import_to_zotero()`） |
| 3 | `ingest_paper` | 🔄 重做 | `mcp_server.py`（加 `pdf_path` + `collection` 参数） |
| 4 | `list_collections` | 🔄 重做 | `mcp_server.py` + `zotero_sync.py`（同时返回 Zotero 文件夹 + ChromaDB + 同步状态） |
| 5 | `create_collection` | 🆕 新增 | `zotero_sync.py` + `vector_store.py`（同时创建 Zotero 文件夹 + ChromaDB collection） |
| 6 | `search_papers` | ➖ 不变 | — |
| 7 | `discover_papers` | ➖ 不变 | — |
| 8 | `get_paper_chunks` | ➖ 不变 | — |
| 9 | `expand_context` | ➖ 不变 | — |
| 10 | `read_paper_full` | ➖ 不变 | — |
| 11 | `get_bibtex` | 🔄 改造 | `mcp_server.py` + `zotero_sync.py`（Zotero 优先 + ChromaDB 语义推荐 fallback） |

**删除：**
- ~~`compare_papers`~~ — Agent 用 `search_papers` + `read_paper_full` 自己做更灵活
- ~~`fetch_and_ingest`~~ — 已拆分为 `download_paper` + `import_to_zotero` + `ingest_paper`

---

#### 4.2 每个工具的详细设计

##### 4.2.1 `download_paper` 🆕

**职责：** 纯下载 PDF，不碰 Zotero 不碰 ChromaDB。

**参数：**
```python
{
    "doi": str,           # 与 title 二选一，优先 doi
    "title": str,         # 可选，用于 discover 找 DOI
    "save_dir": str,      # 可选，默认 data/downloads/
}
```

**返回：**
```json
{
    "pdf_path": "F:/MyProjects/zotero-brain/data/downloads/doi_10_1038_xxx.pdf",
    "source": "openalex",          # cache|openalex|unpaywall|core|arxiv|scihub|none
    "paper": { ... }               # discover 返回的 metadata（title, doi, authors, year 等）
}
```

**实现：**
- `mcp_server.py` 中新增工具定义和 handler
- 如果只有 `title`，先调 `paper_discovery.discover()` 找到 DOI
- 复用 `paper_importer.download_pdf()` 的 6 级瀑布
- 返回 PDF 路径 + 元数据，Agent 可以拿这个路径去做下一步（导入 Zotero 或 ingest）

##### 4.2.2 `import_to_zotero` 🆕

**职责：** 将 PDF + metadata 导入 Zotero，创建条目 + linked_file 附件。纯 Zotero 操作。

**参数：**
```python
{
    "title": str,           # 必填
    "doi": str,             # 可选
    "authors": [str],       # 可选，["Last First", ...]
    "year": int,            # 可选
    "abstract": str,        # 可选
    "url": str,             # 可选
    "pdf_path": str,        # 可选，本地 PDF 路径（创建 linked_file）
    "collection": str,      # 可选，Zotero 文件夹中文名
}
```

**返回：**
```json
{
    "item_key": "ABC12345",
    "collection": "钠电层状氧化物正极",
    "linked_file": true
}
```

**实现：**
- 复用 `paper_importer.import_to_zotero()` 的逻辑
- 接受完整 metadata（不再依赖 discover 的 paper dict 格式）
- 去重：先按 DOI 在 Zotero 搜索，已存在则返回现有 key

##### 4.2.3 `ingest_paper` 🔄 重做

**职责：** 解析 PDF → chunk → embed → ChromaDB。

**参数：**
```python
{
    "zotero_key": str,      # Zotero key（从 Zotero 拉 PDF + metadata）
    "pdf_path": str,        # 或 本地 PDF 路径（跳过 Zotero 下载）
    "collection": str,      # 目标 Collection 中文名
    "force": bool,          # 强制重新解析
}
```

**变更点：**
- 新增 `pdf_path` 参数 — Agent 可以先 `download_paper()` 再传路径进来，不必经过 Zotero
- 新增 `collection` 参数 — 明确指定目标 Collection（不再依赖 Zotero item 的 collection_names）
- `zotero_key` 和 `pdf_path` 二选一：
  - `zotero_key`：从 Zotero 拉 metadata + 下载 PDF
  - `pdf_path`：直接用本地文件，metadata 从 Zotero key 拉或从 PDF 文件名推断

**实现：**
- 修改 `_ingest_paper()` 内部函数，支持 `collection` 参数覆盖 `item["collection_names"]`

##### 4.2.4 `list_collections` 🔄 重做

**职责：** 同时返回 Zotero 文件夹列表 + ChromaDB collection 列表 + 同步状态。

**返回格式：**
```json
{
    "zotero_folders": [
        {"key": "ABC123", "name": "钠电层状氧化物正极", "item_count": 45},
        {"key": "DEF456", "name": "固态电解质", "item_count": 30}
    ],
    "chroma_collections": [
        {"name": "sodium-layered-oxide-cathode", "display_name": "钠电层状氧化物正极", "chunks": 1234},
        {"name": "solid-electrolyte", "display_name": "固态电解质", "chunks": 890}
    ],
    "sync_status": {
        "钠电层状氧化物正极": {"zotero_key": "ABC123", "chroma_name": "sodium-layered-oxide-cathode", "synced": true},
        "固态电解质": {"zotero_key": "DEF456", "chroma_name": "solid-electrolyte", "synced": true},
        "新文件夹": {"zotero_key": "GHI789", "chroma_name": null, "synced": false}
    }
}
```

**实现：**
- `zotero_sync.py` 新增 `list_folders()` 函数 — 返回 Zotero 文件夹 + 每个文件夹的论文数量
- `mcp_server.py` 中合并两个列表，计算 sync_status
- 通过 `collection_map.json` + ChromaDB collection metadata 中的 `display_name` 做映射

##### 4.2.5 `create_collection` 🆕

**职责：** 同时创建 Zotero 文件夹 + ChromaDB collection，Agent 提供中文名和英文 slug。

**参数：**
```python
{
    "folder_name": str,     # Zotero 文件夹中文名（如 "钠电层状氧化物正极"）
    "chroma_name": str,     # ChromaDB 英文 slug（如 "sodium-layered-oxide-cathode"）
}
```

**返回：**
```json
{
    "zotero_folder_key": "ABC123",
    "chroma_collection": "sodium-layered-oxide-cathode",
    "message": "已创建: 钠电层状氧化物正极 ↔ sodium-layered-oxide-cathode"
}
```

**实现：**
- `zotero_sync.py` 新增 `create_folder(name)` — 调用 `zot.create_collections()`
- `vector_store.py` 已有 `_get_collection()` 会自动创建
- 映射关系写入 `collection_map.json` + ChromaDB collection metadata `zotero_folder_key` 字段
- 校验 `chroma_name` 合法性（`[a-z0-9._-]`，3-512 字符）

##### 4.2.6 `get_bibtex` 🔄 改造（保留知识库语义推荐）

**职责：** 生成 BibTeX 引用 + 支持 Agent 辅助写作时的语义引用推荐。

**设计决策：** 用户确认 Agent 辅助写作时按语义推荐引用非常重要，必须保留知识库支持。

**双模式设计：**

**模式 A — 精确引用（默认）：** Agent 已知要引用哪篇论文，提供 identifier
```python
{
    "identifier": str,    # 论文标题、DOI 或 Zotero key
    "mode": "exact",      # 默认
}
```
- **优先从 Zotero API 拉 metadata**（通过 `zotero_sync.py` 的 `list_items()` 缓存 + DOI/标题/key 匹配）
- 如果 Zotero 没有 → fallback 到 ChromaDB metadata
- 如果都没有 → fallback 到 CrossRef API（通过 DOI 查）
- 确保论文没入库（ChromaDB）也能生成 BibTeX（只要在 Zotero 里）

**模式 B — 语义推荐（新增）：** Agent 正在辅助写作，需要根据内容推荐引用
```python
{
    "query": str,          # 当前写作内容的描述或关键词
    "mode": "recommend",   # 语义推荐模式
    "n_results": int,      # 推荐数量，默认 5
    "collections": [str],  # 可选，限定在特定 Collection 中推荐
}
```
- 走 `search_papers()` 的语义搜索逻辑
- 返回匹配的论文列表 + 每篇的 BibTeX
- Agent 可以根据相关性选择引用哪些

**返回格式（统一）：**
```bibtex
@article{wang_2024,
  title={...},
  author={...},
  year={2024},
  doi={10.1038/...},
  journal={...}
}
```

**实现：**
- `zotero_sync.py` 新增 `get_item_metadata(identifier)` — 从 Zotero API 拉单篇论文完整 metadata（包括 journal、volume、pages 等 BibTeX 需要的字段）
- `mcp_server.py` 中根据 `mode` 分发：
  - `exact` → `get_item_metadata()` → 生成 BibTeX（Zotero 优先，ChromaDB fallback）
  - `recommend` → `vector_store.search()` → 对每篇结果生成 BibTeX

##### 4.2.7 删除 `compare_papers`

- 从 `mcp_server.py` 中移除工具定义和 handler
- Agent 可以用 `search_papers` + `read_paper_full` 自行组合做对比，更灵活

##### 4.2.8 删除 `fetch_and_ingest`

- 从 `mcp_server.py` 中移除工具定义和 handler
- 功能已由 `download_paper` + `import_to_zotero` + `ingest_paper` 三个工具覆盖

---

#### 4.3 砍掉 DeepSeek 依赖

**涉及文件：** `config.py`

**当前状态：**
```python
DEEPSEEK_API_KEY = _e("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _e("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = "deepseek-v4-pro"
```

**改动：**
1. 删除 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 三个变量
2. `translate_collection_name()` 不再调 DeepSeek 翻译 → 改为纯查映射表：
   ```python
   def translate_collection_name(chinese_name: str) -> str:
       """中文 Collection 名 → ChromaDB 安全名（查映射表）"""
       if not chinese_name or chinese_name == "uncategorized":
           return "uncategorized"
       if chinese_name in _NAME_MAP:
           return _NAME_MAP[chinese_name]
       # 未找到映射 → 报错，要求先用 create_collection 创建
       raise ValueError(
           f"Collection '{chinese_name}' 未找到映射。"
           f"请先用 create_collection(folder_name='...', chroma_name='english-slug') 创建。"
       )
   ```
3. `create_collection` 工具负责写入映射（Agent 提供英文 slug，不需要 API 翻译）
4. 现有 `collection_map.json` 中的映射数据保留（向后兼容已入库的论文）

---

#### 4.4 ChromaDB Collection 命名策略

- ChromaDB 只接受 `[a-zA-Z0-9._-]`，不支持中文
- **砍掉 DeepSeek**，由 Agent 翻译英文 slug（Skill 指导命名规范）
- `create_collection(folder_name="钠电层状氧化物正极", chroma_name="sodium-layered-oxide-cathode")` 一步到位
- 映射关系存储在：
  - `data/collection_map.json`（向后兼容，快速查找）
  - ChromaDB collection metadata 中新增 `zotero_folder_key` 字段（双向关联）
- 所有接收 `collection` 参数的工具统一按 Zotero 文件夹名查找，内部查映射表得到 ChromaDB name

---

#### 4.5 Skill 串联流程

```
场景 A: "帮我找最新的钠电正极论文并入库"
  1. list_collections() → 看用户有哪些 Zotero 文件夹 + 同步状态
  2. discover_papers(query="...") → 搜论文
  3. Agent 判断论文属于哪个文件夹（语义匹配）
     - 匹配上了 → download_paper(doi=...) → import_to_zotero(collection="钠电层状氧化物正极") → ingest_paper(zotero_key=...)
     - 没匹配上 → 问用户："这篇放哪个文件夹？还是新建一个？"
     - 用户想新建 → create_collection(folder_name="新名字", chroma_name="english-slug")
  4. search_papers() → 验证入库成功

场景 B: "我已经有这篇论文的 PDF 了，帮我入库"
  1. list_collections() → 看有哪些文件夹
  2. Agent 判断放哪个文件夹，不确定就问用户
  3. import_to_zotero(title=..., pdf_path="C:/.../paper.pdf", collection="钠电层状氧化物正极")
  4. ingest_paper(pdf_path="C:/.../paper.pdf", collection="钠电层状氧化物正极")

场景 C: "只下载到本地，先不入库"
  1. download_paper(doi="...") → 返回 PDF 路径
  2. 用户随时可以：import_to_zotero(...) + ingest_paper(...)

场景 D: 首次批量入库（run_ingest.py）
  1. 遍历 Zotero 所有文件夹
  2. 每个文件夹检查 ChromaDB 是否有对应 collection → 没有则 create_collection()
  3. 按文件夹逐个 ingest

场景 E: Agent 辅助写作（语义引用推荐）
  1. get_bibtex(query="关于 LLZO 电解质界面稳定性的讨论", mode="recommend", n_results=5)
  2. 返回 5 篇最相关论文 + 每篇的 BibTeX
  3. Agent 根据上下文选择引用哪些
```

---

#### 4.6 实施步骤（按顺序执行）

| 步骤 | 改动 | 文件 | 预计影响 |
|------|------|------|----------|
| **Step 1** | git commit 存档 | .git | ✅ 已完成 (commit `3c6050a`) |
| **Step 2** | 砍 DeepSeek：删除 DEEPSEEK_* 配置，`translate_collection_name()` 改为纯查映射表 | `config.py` | 低风险，映射表已有数据 |
| **Step 3** | `zotero_sync.py` 新增 `list_folders()` + `create_folder()` + `get_item_metadata()` | `zotero_sync.py` | 新增函数，不影响现有 |
| **Step 4** | `vector_store.py` 新增 `create_collection()` 函数（含 metadata 写入） | `vector_store.py` | 新增函数 |
| **Step 5** | 重写 `mcp_server.py` 工具层：新增 3 个工具 + 改 3 个工具 + 删 2 个工具 | `mcp_server.py` | **核心改动** |
| **Step 6** | 改 `run_ingest.py`：按 Zotero 文件夹自动对齐 ChromaDB collection | `run_ingest.py` | 小改 |
| **Step 7** | `py_compile` 全部 .py 文件语法检查 | 所有 | 验证 |
| **Step 8** | 写 Skill（SKILL.md）：指导 Agent 使用这套工具的工作流 | 新建 | 用户侧 |
| **Step 9** | git commit Phase 4 改动 | .git | 存档 |
| **Step 10** | 更新 README.md + PROJECT.md | 文档 | 最后更新 |

---

#### 4.7 进度表

| 步骤 | 状态 |
|------|------|
| Step 1: git 存档 | ✅ |
| Step 2: 砍 DeepSeek | ✅ |
| Step 3: zotero_sync 新函数 | ✅ |
| Step 4: vector_store 新函数 | ✅ |
| Step 5: mcp_server 工具层重写 | ✅ |
| Step 6: run_ingest 改造 | ✅ |
| Step 7: 语法检查 | ✅ 全部 12 个 .py 通过 |
| Step 8: Skill 编写 | ✅ ~/.workbuddy/skills/zotero-brain/ |
| Step 9: git commit | ⏳ |
| Step 10: 文档更新 | ✅ |

**预估工作量**：1 个会话

---

### Phase 2：报告生成 ⏳ 待开工

**目标**：LLM 把论文变成普通人能看懂的图文并茂 HTML 报告。

**管线：**
```
Phase 1 搜索结果 / 指定论文
    ↓
读取全文 Markdown（从 parsed/ 缓存或 ChromaDB 检索）
    ↓
Agent 直接生成通俗版解读 + 关键数据提取（不再需要 DeepSeek）
    ↓
HTML 模板渲染（带 CSS 美化、图表、表格）
    ↓
输出到 D:\temp_files\ 或指定目录
```

**Phase 2 能力清单：**

| 你能做什么                                            | MCP 工具            |
| ----------------------------------------------------- | ------------------- |
| "把这篇论文通俗地解释给我听" → 输出 HTML               | Agent 直接生成      |
| "把这 5 篇论文的关键数据汇总成对比表" → HTML 报告      | Agent + search 组合 |
| 排版精美，有图表、表格、公式                           | HTML 模板渲染       |

**预估工作量**：1-2 天

---

### Phase 3：自动化 📅 后续

**目标**：从"手动触发"升级为"自动运转"。

| 自动化场景                         | 实现方式                               |
| ---------------------------------- | -------------------------------------- |
| 新论文入库 → 自动解析 → 自动向量化 | 定时扫描 Zotero 新增 key，自动跑 ingest |
| 定期生成"领域动态简报"             | 周报/月报脚本，汇总最新入库论文的摘要   |
| 写论文时主动推荐引用               | Agent 根据你正在写的内容，自动检索推荐  |
| 新论文下载即译                     | 可选：入库后自动生成中文解读 HTML       |

**预估工作量**：按需逐步实现

---

## 文件结构

```
F:\MyProjects\zotero-brain\
│
├── .env                    # API 密钥（不提交 git）
├── .gitignore
├── config.py               # 配置读取（.env → Python 变量）
│
├── zotero_sync.py          # Zotero Web API：拉论文列表、下载 PDF
├── pdf_parser.py           # MinerU VLM：PDF → 结构化 Markdown
├── chunker.py              # 文本切块：按章节切 500-1500 字块
├── embedder.py             # 智谱 Embedding-3：文本 → 2048 维向量
├── vector_store.py         # ChromaDB 多集合管理 + exists_by_metadata() 精确查重
├── paper_discovery.py      # 论文发现：OpenAlex(主力) / arXiv / CrossRef / Semantic Scholar + Unpaywall OA
├── paper_importer.py       # 自动入库：下载 → Zotero → MinerU → ChromaDB + 双重去重
├── network_helper.py       # MinerU TUN 绕过：DoH + httpx 猴子补丁 + CDN 子域名独立解析
├── mcp_server.py           # MCP 服务：暴露 10 个工具给 AI Agent
├── ingest_resume.py        # 增量补全脚本（跳过已解析的）
│
├── data/
│   ├── chroma_db/          # ChromaDB 持久化数据
│   ├── collection_map.json # Zotero 中文名 → ChromaDB 英文名缓存
│   ├── last_ingest_stats.json  # 上次入库统计
│   └── download_*.pdf      # PDF 本地缓存（DOI 命名）
│
├── parsed/                 # MinerU 解析缓存（每个论文一个子目录）
│   └── {ZOTERO_KEY}/
│       ├── {ZOTERO_KEY}.md   # 解析后的 Markdown
│       └── {ZOTERO_KEY}.pdf  # 原始 PDF
│
└── .venv/                  # Python 虚拟环境
```

---

## 技术栈

| 组件         | 用什么                          | 说明                       |
| ------------ | ------------------------------- | -------------------------- |
| Zotero 读写  | pyzotero + Zotero Web API v3    | 免费，需 User ID + API Key |
| PDF 解析     | MinerU SDK（VLM 模型）          | 结构化 Markdown，含表格公式 |
| LLM 推理     | DeepSeek V4 Pro                 | 论文分析、总结、对比       |
| 文本向量化   | 智谱 Embedding-3（2048 维）     | 云端 API，不占本地 GPU     |
| 向量数据库   | ChromaDB（本地 PersistentClient）| 纯文件存储，无需服务器     |
| Agent 接口   | MCP (Model Context Protocol)    | stdio 通信，被 IDE 调用    |

---

## MCP Server 提供的工具

### 文献检索与阅读

| 工具名              | 用途                                                    |
| ------------------- | ------------------------------------------------------- |
| `search_papers`     | 语义搜索文献库（可限定 Collection + `paper_keys` 单篇锁）|
| `get_paper_chunks`  | 列出论文 chunk 目录（编号+章节+摘要），了解结构          |
| `expand_context`    | 围绕某个 chunk 扩展上下文（前N后N个 chunk 完整文本）     |
| `read_paper_full`   | 读取单篇论文全文（从 parsed/ 缓存，不重新解析 PDF）      |

### 论文对比与引用

| 工具名              | 用途                                          |
| ------------------- | --------------------------------------------- |
| `compare_papers`    | 对比多篇论文的方法、结论等                    |
| `get_bibtex`        | 生成 BibTeX 引用格式                          |

### 文献库管理

| 工具名              | 用途                                          |
| ------------------- | --------------------------------------------- |
| `list_collections`  | 列出所有 Collection 及其 chunk 数量           |
| `ingest_paper`      | 入库单篇论文（提供 Zotero key）               |

---

## 如何运行

### 环境要求

- Python 3.13（虚拟环境已配置）
- 依赖已在 `.venv/` 中安装，包括：`pyzotero`, `chromadb`, `httpx`, `mineru`, `mcp`

### 日常使用（通过 IDE Agent）

Agent 会自动启动 MCP Server，你只需要用自然语言对话：

```
"我文献库里有哪些关于 LLZO 电解质的论文？"
"对比一下 Wang 2024 和 Li 2025 这两篇的方法"
"帮我生成这篇论文的 BibTeX"
"文献库里有哪些 Collection？"
"那篇 JT 综述里，mitigation strategies 怎么分类的？——先 get_paper_chunks 看结构，再 expand_context 深入"
"把这篇论文全文调出来，我要逐段讨论" → read_paper_full
```

### 手动入库（终端）

```powershell
cd F:\MyProjects\zotero-brain
.venv\Scripts\python.exe ingest_resume.py    # 增量补全（只处理新增论文）
```

### 手动启动 MCP Server

```powershell
cd F:\MyProjects\zotero-brain
.venv\Scripts\python.exe mcp_server.py
```

---

## ⚠️ 使用注意事项：TUN 模式

**`network_helper.py` 通过 DoH + httpx 猴子补丁让 MinerU 流量走国内直连。** 该补丁仅影响 `httpx` 库，不影响其他 MCP 连接器（携程问道、IMA、腾讯会议等已通过 WorkBuddy 内置代理独立通信）。

**已确认**：WorkBuddy 内置连接器（ima知识库、携程问道、腾讯会议）各自独立工作，不受 TUN 模式影响。`connector-proxy` 已删除（冗余配置）。

**`network_helper.py` 的作用域**：仅在 Zotero Brain 进程内部生效（`httpx.Client._send_single_request`），进程退出后自动卸载。

---

## 设计决策

1. **不用本地 GPU**：所有 embedding 走智谱云端 API（¥0.0007/千 token），不折腾本地模型
2. **自动分库**：复用 Zotero 的 Collection 分类，中文名自动翻译为 ChromaDB 安全名
3. **增量更新**：`ingest_resume.py` 检查已有 key，只处理新增论文，不重复解析
4. **PDF 优先本地**：先从 `~/Zotero/storage/` 复制，找不到才走云端下载
5. **MCP 协议**：选了 MCP 而不是 REST API，因为 AI Agent 生态已原生支持

---

## 下一步行动

**当前状态**：Phase 1 ✅ → Phase 1.5 ✅ → Phase 2.5 ✅ → **代码审查 & 发布准备 📋 进行中**

> MCP Server 功能开发已基本完成。下一阶段：代码审查、整理、补全发布文件，准备 Push 到 GitHub。

**发布准备清单：**
1. ~~**代码审查** — 12 个 .py 文件过一遍，清理冗余、统一风格、删死代码~~ ✅ (2026-06-12)
   - 清理 6 个 unused imports (zotero_sync: os/json, paper_importer: Any/zotero, mcp_server: json, paper_discovery: Any)
   - 移除 `mineru` SDK 依赖 → `pdf_parser.py` + `run_ingest.py` 全部改用 raw httpx REST API
   - 12/12 .py 文件 `py_compile` 语法检查全部通过
2. ~~**补全发布文件** — `README.md`、`requirements.txt`、`.gitignore` 检查~~ ✅ (2026-06-12)
   - `README.md` 已创建（项目介绍、安装、配置、用法、项目结构）
   - `requirements.txt` 已清理（移除 mineru/requests，加入 pypdf）
3. **Push 到 GitHub** — 待用户确认

> 原则："代码能跑就不要动"——只做必要的清理和补全，不做功能性重构。

**后续扩展（按需，非 MCP Server 本身）：**
- 自动化流程（定时搜索新论文、领域周报）→ 用户通过 Skill/Automation 自定义
- 报告生成（`explain_paper`、`generate_review`）→ 按需追加 MCP 工具
- Semantic Scholar API key 申请（免费，需机构邮箱）

详见上方 [三阶段路线图](#三阶段路线图)。
