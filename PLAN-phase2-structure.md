# 代码审查 +「结构化 / 解读 / 翻译」规划

> 2026-07-22。本文档 = 一次全库代码 review 的发现 + 对"要不要结构重构、谁来干、图片错位怎么办"三个问题的结论 + 分阶段实施计划。
> 仅规划，未动代码。确认后按第七节分阶段执行，执行时把对应阶段合并进 PROJECT.md 路线图。

---

## 一、代码审查：先于一切新功能要修的 bug

这些与"结构化"无关，但**直接影响解析正确性**，其中两条可能就是你觉得"MinerU 图片分块不对"的部分原因。

### P0 — 影响数据正确性，必修

| # | 位置 | 问题 | 后果 | 修法 |
|---|------|------|------|------|
| 1 | `pdf_parser.py:62-64` | MinerU 分片的页码范围写进了 `data_id` 字段，v4 API 的正确字段是 `file.page_ranges` | >200 页 PDF 分片静默失效：每片都上传整份 PDF 且不带页码约束，MinerU 直接报错，重试 3 次后整篇失败。目前 <200 页论文恰好 `chunks_needed==1`，bug 被掩盖 | 改用 `page_ranges`；`data_id` 留作业务标识（见 #3） |
| 2 | `pdf_parser.py:201-206`、`run_ingest.py:122-127` | 提交选项写的是 `{"model": ..., "formula": true, "table": true}`，官方字段疑似为 `model_version / enable_formula / enable_table` | **若 API 忽略未知字段，`MINERU_MODEL=vlm` 从未生效，一直跑的是默认 pipeline 模型**——"选最新模型"就无从谈起。抽样缓存里有 VLM 风格的图表描述，不排除 API 接受别名，**需实测确认** | 对照 MinerU 当前 API 文档核实字段名并修正；顺便确认可选的最新模型版本 |
| 3 | `run_ingest.py:165-179` | 批量解析结果用 `item_keys[i]` 按下标配对，但 API 不保证返回顺序；批量模式 `data_id` 为空、无法校验 | 一旦乱序，**A 论文的 Markdown 会写进 B 论文的缓存目录**并入库，且被缓存机制永久固化，极难发现 | 提交时设置 `data_id = item_key`，下载结果时按 `data_id` 配对 |

> **✅ P0 已修复 (2026-07-22)**：字段名对照 [MinerU 官方文档](https://mineru.net/apiManage/docs) 全部核实并修正（`model_version` / `enable_formula` / `enable_table` / 每文件 `page_ranges` / `data_id` 配对）；顺带修复 `run_ingest.py` 轮询仍用旧版 `{success}` 响应格式的问题（新版 API 返回 `{code: 0}`，批量轮询会立即误抛异常）。实测：2.1MB PDF 以 `model_version=vlm` + `page_ranges=1-2` + `data_id` 提交，15 秒解析完成、`data_id` 正确回传；zip 产物确认含 `full.md` + `*_content_list.json` + `*_content_list_v2.json` + `layout.json` + `images/`（P1 前提成立）。>200 页真实分片与批量乱序配对留待下次批量入库时观察验证。

### P1 — 健壮性

| # | 位置 | 问题 |
|---|------|------|
| 4 | `run_ingest.py:281-283` vs `:69-75` | `_has_cached_parse()` 接受 stem 命名的缓存，真正读取时只认 `{key}.md` → 命中 stem 缓存时 FileNotFoundError，Phase 1 直接崩 |
| 5 | `run_ingest.py:186-191` | MinerU 批次超时时，已 `done` 的结果**不下载**，全部标 None 浪费配额 |
| 6 | `ingest_resume.py:90-97` | 单篇 chunk/embed/入库无 try/except，一篇抛异常整个补全脚本中断；且 `:43-47` 跨 collection 聚合 known_keys，论文在 A 库成功 B 库失败时被误判"已入库"跳过 |
| 7 | `vector_store.py:371` | 迁移 collection 用 `get_or_create_collection(name=...)` 未传 `metadata={"hnsw:space": "cosine"}`，迁移后的库用默认 l2 距离，与其他库打分口径不一致 |
| 8 | `paper_importer.py:520-546` | `_parse_authors` 把 2-token 名按 "Last First" 解析，但 OpenAlex/S2 的 display_name 是 "First Last" → **姓名颠倒写入 Zotero**，污染 BibTeX |
| 9 | `zotero_sync.py:362-365` | 附件文件名不匹配时 fallback 抓 `papers_dir` 里任意 >1000 字节的 PDF，可能错配别人的 PDF |

### P2 — 质量改进（不紧急）

- `embedder.py:18` 全局 httpx client 永不关闭；`:24-25` 429/5xx 不重试、限流只靠固定 sleep 0.3s；`:55-56` 返回条数不足时 zip 静默丢尾部 chunk 且无日志。
- `vector_store.py:224` `_find_col_for_paper` 只返回第一个命中的 collection → 论文入多库时 `get_paper_chunks`/`expand_context` 漏数据，连带 `_ingest_paper` 去重（`mcp_server.py:78-82`）会阻止论文补进新归属的 collection。
- `mcp_server.py:1212-1222` `read_paper_full` 无分页/截断，整篇 3-8 万字符一次性返回（设计缺口，见 P1 阶段处理）。
- `mcp_server.py:1112-1118` `get_bibtex` exact 模式的 ChromaDB fallback 用语义搜索 top-1 当"精确匹配"，标题相近时可能张冠李戴。
- `zotero_sync.py:604-607` `get_item_metadata` 标题精确匹配失败后返回第一个候选，可能拿到完全错误的论文。
- 三处重复的 ingest 管线（`mcp_server.py:56-141` / `run_ingest.py` / `ingest_resume.py`），metadata dict 构造逐字重复，改一处必漏另一处 → 值得收敛成一个共享函数。
- `config.py:80-81` `_save_name_map` 非原子写，崩溃截断后 `:69` 无 catch → 模块 import 直接失败。
- `network_helper.py:133` 注释称 doh.pub "is overseas"——doh.pub 是腾讯国内服务，注释错误（不影响功能）。
- 做得好的地方：MCP 工具错误信息中文+结构化、`call_tool` 全异常兜底、`expand_context` 有上下限钳制、批量工具 description 自觉建议 >30 篇走 CLI。

---

## 二、核心问题：OCR 后直接入库，要不要加"结构重构"？

### 现状事实

现在确实是"MinerU Markdown → 扁平按标题切块 → 直接入库"，中间**没有任何结构层**：

- `mcp_server.py:104-123`：parse 返回的 md 字符串直接进 `chunker.chunk_markdown()`。
- `chunker._split_by_headings`（`chunker.py:77-103`）只识别 `#{1,3}`，产出扁平 `(标题, 正文)` 对，**不保留层级**（不知道 `## 1.1` 属于 `# 1`）。
- MinerU zip 里其实有 `*_content_list.json`（含 `text_level` 标题层级、`page_idx`、bbox、图片 caption 配对），`pdf_parser._download_results`（`pdf_parser.py:140-145`）**只抽了 .md 和图片，JSON 全丢了**。

### 你的判断基本对，但有两个"不是小错"的例外

你的推理——"看英文扫描结果的只有 Agent，Agent 不怕异常分段"——对 **RAG 检索路径**成立。Agent 确实对轻微的段落错分无感，为检索路径引入 LLM 结构分析是**过度工程**。

但 review 发现当前有两个**内容丢失级**的问题，不是"细微结构错误"：

1. **纯图/纯表 chunk 被整块丢弃**：`![](images/xx.jpg)` + `<details>` 图表描述通常 <200 字，被 `chunker.py:72` 的 `min_chunk_size` 过滤掉——论文的图表内容在向量化阶段就丢了，Agent 搜不到也读不到。
2. **向量被污染**：图片与正文同 chunk 时，`![](images/...)` 相对路径和 `<details><summary>` HTML 标签原文进入 embedding，稀释语义。

这两个问题**不需要 LLM 就能修**（见 P1 阶段），但它说明"OCR 直接入库"目前连免费的信息都没保住。

### 结论

- **不加 LLM 结构分析层**。对论文场景它是过度工程（理由见第三节：论文编号自带绝对层级，不像书籍需要 LLM 校准）。
- **加"零成本结构信号保留"**：把 MinerU zip 里免费的 `content_list.json` 落盘，chunker 用它补齐层级/图注配对/页码，修掉上面两个内容丢失。
- 结构信号同时为后续**全文翻译、太奶解读、图文排版**提供底座——这些生成类功能确实需要结构，但结构来源是 MinerU 的既有产物，不是新增 API。

---

## 三、Books_Converter：引入什么、不引入什么

Books_Converter v4 的核心思想值得照搬：**MinerU 只当 OCR，结构判断全部外置**。但它的"重武器"是为书籍设计的，论文用不上。

### 它的结构重构到底是什么

输入是 MinerU 的 `content_list.json`（不是 markdown——markdown 生成后下游基本不用），然后四步：

1. 纯规则筛候选（拼段/标题/图注/跨页表格四类，`stage2_hybrid.py:167-171` + `popo/inference.py`）；
2. LLM（DeepSeek）逐块回答 4 个局部问题（12 页/块，561 页全书 ~140 次调用 ≈ ¥0.15）；
3. **TOC 锚点 + 编号形状栈**纯代码定全局层级（`stage2_common.py:229-391`）——因为书测发现 LLM 给的绝对层级会漂移到 L15，永远不可信；
4. 栈式建树（`popo/tree.py:53-207`）。

### 引入清单

| 模块 | 复用价值 | 在 zotero-brain 的用途 |
|------|---------|------------------------|
| `content_list.json` 落盘（它 stage1 的产出） | ★★★ 前提 | 一切结构能力的来源，零成本 |
| 图文关联机制（caption 归属判定 + bbox 包含链接 + **孤儿 caption 按普通段落兜底**，`stage3_epub.py:941-947`） | ★★★ 论文刚需 | 修"图注与图分离、图片内容丢失"；"绝不丢内容"原则直接继承 |
| `popo/convert.py` content_list → pages 转换 | ★★★ 基本可直搬 | 结构增强的输入层；删掉书籍特有的"编分隔页捞回"即可 |
| `stage4_translate.py` 整文件 | ★★★ 以后直搬 | 批量全文翻译：~6000 字/批、附前批 400 字上下文、滚动术语表、每批落盘断点续翻、失败拆半自救。论文只有 2-5 批 |
| `stage3_epub.py` 的 MathML 公式 + 图文渲染底座 | ★★★ 改造后用 | 解读/翻译的 HTML 排版底座；`_dehyphen_join` 跨页断词修复也通用 |
| 跨页段落拼接（contd 标注） | ★★ | 让 chunk 不再在段落中间硬断（现在 `chunker.py:135` 会截在公式/表格行中间） |
| 跨页表格合并（`popo/table_merge_*`，6 项启发式检查） | ★★ | 论文跨页表常见，检查规则与文档类型无关 |

### 不引入清单

| 模块 | 原因 |
|------|------|
| TOC 锚点校准 | 论文没有目录页 |
| 编号形状栈（12 种形状 + 大纲栈推理） | 那是为"标题无绝对编号"的书籍设计的；论文的 `1 / 1.1 / 1.1.1` 编号**自带绝对层级**，直接算深度即可，比书籍简单一个数量级 |
| 重页检测 / 编分隔页救援 | 扫描书特有缺陷 |
| 书籍级 LLM 逐块结构判断 | 论文 10-30 页，结构信号（编号 + MinerU text_level）已足够，不需要 |

---

## 四、谁来干：接 API 还是 MCP Agent？

判断标准是任务性质：**确定性变换**还是**生成式、与用户口味相关**。

| 任务 | 性质 | 谁干 | 理由 |
|------|------|------|------|
| 结构信号保留（content_list 落盘、层级/图注/页码进 chunk） | 确定性 | **管线代码**，零 LLM | 规则明确，做一次永久受益 |
| 太奶解读 / 任意风格解读 | 生成式、千人千面 | **MCP Agent** | 风格、深度、详略都要随对话调整，这正是 Agent 的主场；也符合 Phase 4 定下的"MCP 只做执行，Agent 做决策"原则 |
| 全文翻译（单篇、按需） | 生成式 | **MCP Agent** | 论文 3-8 万字符，Agent 分段翻+拼装即可，用户还能逐段讨论 |
| 全文翻译（批量、入库即译） | 确定性批处理 | **脚本 + 一个便宜 LLM API** | 这时才需要新 API。而且**不用新申请**：你已有的智谱 key 可以直接当 OpenAI 兼容端点用（GLM-4-Flash 免费档），Books_Converter 的 stage4 只要求 OpenAI 兼容协议 |

**结论：不新增"结构分析 API"。** 唯一的 API 增量是可选的批量翻译/解读后台脚本，且可复用现有智谱 key。这和你 Phase 4 砍 DeepSeek 的方向一致——管线保持无 LLM，智能在 Agent 侧。

---

## 五、MinerU 图片错位：三层对策（成本递增，都要）

1. **先修自家 bug，再谈模型**：P0 #2 的字段名问题如果坐实，`vlm` 模型可能从未生效——你以为在用 VLM，实际可能在用 pipeline。修字段 + 对照文档确认当前可选的最新模型版本，这是零成本的第一步。
2. **结构层兜住关联**：`content_list.json` 的 image block 自带 `image_caption` 配对，用它做图注关联后，**不再依赖 block 出现顺序**——MinerU 块序错一点，图注配对依然正确；孤儿图注按正文兜底，绝不丢内容。
3. **多模态 Agent 按需校准**：新增 `get_paper_images` 工具列出 `parsed/{key}/images/` 的图片 + caption + 页码，Agent 生成解读/翻译时**直接看图**（多模态），发现错位可以人工级纠正、给图重新写说明。这比追求完美的 OCR 管线现实得多——**不追求 OCR 零错误，追求"错了也兜得住"**。

---

## 六、新功能设计

### 6.1 论文解读（"太奶模式"及任意定制风格）

```
用户："用 80 岁太奶都能懂的话解读这篇论文"
  ↓
get_paper_structure(paper_key)   → 章节树（来自 content_list.json）
read_paper_full / expand_context → 正文
get_paper_images(paper_key)      → 图片清单（文件 + caption + 页码）
  ↓
Agent（多模态看图 + 读文）按用户指定风格生成解读 HTML
  —— 排版底座借鉴 Books_Converter stage3：MathML 公式、图片+caption、CSS 模板
  ↓
attach_to_zotero(item_key, html_path, title="AI 解读", content_type="text/html")
  → linked_file 附件挂到 Zotero 条目，双击浏览器打开
```

新增 3 个 MCP 工具（都是薄封装）：

- `get_paper_structure(paper_key)` — 从缓存的 content_list.json 返回章节树 + 各节字数，Agent 据此规划阅读；
- `get_paper_images(paper_key)` — 返回 `[{file, caption, page_idx}]`，图片绝对路径供 Agent 用读图工具查看；
- `attach_to_zotero(item_key, file_path, title, content_type)` — 通用 linked_file 附件创建（把 `paper_importer.py:649-667` 里硬编码 PDF 的 payload 抽成通用函数，顺便修掉 `zotero_sync.py:379/406` 只认 PDF 的限制）。

配套一个 Skill（解读工作流 + 风格 prompt 模板），风格可定制：太奶 / 组会汇报 / 审稿人视角 / 逐段精读……

### 6.2 全文翻译

- **单篇按需**：Agent 循环 `get_paper_structure` 分节读取 → 翻译 → 套用同一 HTML 底座渲染 → `attach_to_zotero` 挂附件。零新代码（复用 6.1 的工具和模板），零新 API。
- **批量自动**（可选，"新论文下载即译"）：移植 Books_Converter `stage4_translate.py`（分批/上下文/术语表/断点续传全套现成），后台脚本跑，LLM 端点复用智谱 key。这步等单篇流程验证过排版效果后再做。

### 6.3 Zotero 附件方式说明

- **linked_file**（推荐，与现有 PDF 策略一致）：本地秒开、不占云配额；缺点是不同步到其他设备。
- **imported_file**（pyzotero `attachment_simple`）：跨设备同步，但占 Zotero 免费 300MB 配额。
- 先 linked_file，工具参数留个开关，以后想同步再说。

---

## 七、分阶段实施计划

| 阶段 | 内容 | 验收 | 预估 |
|------|------|------|------|
| **P0 修 bug** ✅ 已完成 (2026-07-22) | 第一节 P0×3 + 轮询响应格式兼容（P1×6 健壮性修复待做） | ✅ 实测通过（见上方修复记录）；>200 页分片与批量配对留待真实批量入库验证 | 实际 0.3 会话 |
| **P1 结构信号保留** | ① `pdf_parser._download_results` 把 `*_content_list.json` 存进 `parsed/{key}/`；② chunker 用 content_list 增强：标题层级进 metadata、图注与图配对、纯图表 chunk 不再丢弃（图表描述替代裸图片引用进正文）、页码进 metadata；③ `read_paper_full` 加 offset/limit 或按章节读取 | 抽 3 篇已入库论文重新 ingest，检查图表 chunk 存在且 caption 配对正确；旧缓存无 content_list 时优雅降级到现有逻辑 | 1 会话 |
| **P2 解读闭环** | 3 个新 MCP 工具（structure/images/attach）+ 解读 Skill + HTML 模板（MathML + 图片 + CSS） | 挑一篇论文生成"太奶解读"，HTML 里公式/图/caption 正确，Zotero 里双击能打开 | 1-2 会话 |
| **P3 批量翻译（可选）** | 移植 stage4_translate，复用智谱 key，入库后自动生成中文译文 HTML 挂附件 | 一篇论文后台翻译完成，术语一致，断点续翻可用 | 1 会话 |

依赖关系：P0 独立且最优先；P1 依赖 P0 的字段修复（否则新解析还是错）；P2 依赖 P1 的 content_list 缓存（旧论文可在 P1 阶段顺手补解析或首次使用时补）；P3 依赖 P2 的 HTML 底座。

### 明确不做的事

- 不给 RAG 检索路径加 LLM 结构分析（过度工程）；
- 不引入 Books_Converter 的 TOC 锚点 / 编号形状栈（论文编号自带层级）；
- 不追求 MinerU 零错位（用 caption 配对 + 多模态 Agent 兜底替代）；
- 不新增"结构分析"专用 API（唯一可选 API 增量复用现有智谱 key）。
