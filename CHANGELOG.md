# Changelog

本项目的所有重要变更都会记录在此文件中。

格式约定：按版本倒序，分「新增 / 修复 / 变更」三类，每条一行、从用户视角描述。
版本号规则：加功能进位中版本（0.x.0），纯修 bug 进位小版本（0.x.y）。

## [0.2.0] - 2026-07-22

### 新增

- 3 个新 MCP 工具（共 14 个）：`get_paper_structure`（章节结构树，层级/页码/每节字数）、`get_paper_images`（图片清单 + caption + 绝对路径，供多模态 Agent 看图）、`attach_to_zotero`（把 AI 解读/翻译 HTML 挂为 Zotero 条目的 linked_file 附件，双击即开）
- 解析缓存新增 `content_list.json`（MinerU 块级结构），切块升级为块级切块：图/表/曲线图成为独立 chunk 不再因过短被丢弃，chunk 带章节层级（只认论文编号，如 1.1.1）和页码
- `read_paper_full` 支持 `offset/limit` 分段读取（全文不再一次性撑爆上下文）
- `data/reports/` 目录约定：Agent 生成的解读/翻译报告统一存放

### 修复

- **MinerU `vlm` 模型此前从未生效**（API 字段名写错，一直跑默认 pipeline），已按官方文档修正并实测验证
- 超过 200 页的 PDF 分片解析静默失效（页码范围写错了字段）
- 批量入库结果可能错配到别的论文（按下标配对，API 不保证顺序）——现按 `data_id` 配对
- 批量入库超时不再丢弃已解析完成的结果；缓存文件名不一致导致的崩溃；作者姓名 "First Last" 被写反污染 BibTeX；Collection 迁移后向量距离口径错误等共 9 处健壮性问题

### 变更

- 切块入口统一为 `chunk_auto`：有 content_list 走块级切块，无缓存自动回退旧逻辑（旧缓存零影响）
- 作者名格式契约统一为 `First Last`（最后一个词为姓），与 OpenAlex/arXiv 等数据源一致

> **升级提醒**：存量论文是旧模型 + 旧逻辑解析的。建议择机重跑
> `.venv\Scripts\python.exe run_ingest.py --no-incremental --force-parse`（全量重新解析 + 重新入库），
> 一次性应用 vlm 模型、新切块与 content_list 缓存。

## [0.1.0] - 2026-06-12

首个可用版本。

- 语义搜索文献库（MinerU 解析 → 智谱 Embedding → ChromaDB，按 Zotero Collection 分库）
- 渐进式深度阅读：`get_paper_chunks` / `expand_context` / `read_paper_full`
- 论文发现与自动入库：OpenAlex/arXiv/CrossRef/S2 多源搜索，6 级下载瀑布，自动去重
- Zotero-First 设计：download → import → ingest 三工具解耦，Collection 中英文映射
- BibTeX 导出（精确引用 + 语义推荐）
- 11 个 MCP 工具，stdio 接入 WorkBuddy / Cursor 等 MCP 客户端
