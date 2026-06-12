# Zotero Brain

> 让你的 Zotero 文献库变成可语义搜索、可 AI 对话的活知识库。

## 这是什么？

Zotero 管理论文很好用，但它本质上是个死数据库——只能按标题、作者、标签搜索，不能按**意思**搜索。

Zotero Brain 解决这个问题：

```
你的 Zotero 文献库
    ↓
MinerU Cloud API（PDF → 结构化 Markdown）
    ↓
文本切块 → 智谱 Embedding 向量化 → ChromaDB 向量数据库
    ↓
MCP Server（11 个工具）
    ↓
AI Agent（WorkBuddy / Cursor / 任何 MCP 兼容客户端）
```

**一句话：** 你用自然语言问 AI "关于钠电正极材料的界面稳定性，我们文献库里有什么相关论文？"，AI 就能从你的 Zotero 里语义搜索、精确定位段落、深入阅读、生成引用。

## 核心能力

| 你想知道/做的事 | 怎么做 |
|---|---|
| "我们库里有哪些关于 LLZO 电解质的论文？" | `search_papers` — 语义搜索，不是关键词搜索 |
| "这篇论文的方法部分具体怎么做的？" | `get_paper_chunks` + `expand_context` — 精确定位并深入阅读 |
| "把这篇论文全文调出来，我要逐段讨论" | `read_paper_full` — 读取完整 Markdown 缓存 |
| "帮我找最新的钠电正极论文" | `discover_papers` — 搜 OpenAlex/arXiv/CrossRef/Semantic Scholar |
| "找到一篇好论文，帮我下载并入库" | `download_paper` → `import_to_zotero` → `ingest_paper` — 三步完成 |
| "我正在写论文，帮我推荐相关引用" | `get_bibtex(mode="recommend")` — 语义推荐 + BibTeX |
| "这篇论文的 BibTeX 是什么？" | `get_bibtex(mode="exact")` — 从 Zotero 拉取完整引用信息 |
| "我的文献库有哪些文件夹？哪些已同步？" | `list_collections` — 同时显示 Zotero 文件夹 + 向量库状态 |
| "新建一个文件夹来放新方向的论文" | `create_collection` — 同时创建 Zotero 文件夹 + 向量库 |

## 使用场景

### 场景 1：快速查阅文献库

你和 AI 说："关于火焰喷雾热解合成纳米颗粒，库里有什么论文讨论了前驱体浓度对粒径的影响？"

AI 会语义搜索你的文献库，找到相关段落，展开上下文给你详细解释。不需要你自己一篇篇翻。

### 场景 2：新论文入库

你说："帮我找 2025 年关于 self-driving lab 的最新论文，下载并入库到'自动化实验室'文件夹。"

AI 搜索学术数据库 → 下载 PDF → 导入 Zotero → OCR 解析 → 向量化入库。一条命令搞定。

### 场景 3：AI 辅助写作

你在写论文的 Introduction，说："关于自动化实验室在材料合成中的应用，帮我推荐引用。"

AI 语义搜索你的文献库 → 找到最相关的论文 → 深入阅读关键段落 → 给你推荐 + BibTeX + 段落摘要。

### 场景 4：精准对比

你说："对比一下 Wang 2024 和 Li 2025 这两篇关于固态电解质的方法和结论。"

AI 分别读取两篇论文的相关段落，提取关键信息进行结构化对比。

## 安装

### 前置条件

- **Python 3.13+**（推荐用 venv 虚拟环境）
- **Zotero**（本地安装，有你的文献库）
- **能翻墙的网络**（MinerU、OpenAlex 等 API 需要访问境外服务器，见下方网络配置）

### 步骤 1：克隆项目

```bash
git clone <your-repo-url>
cd zotero-brain
```

### 步骤 2：安装依赖

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 步骤 3：配置 API 密钥

复制 `.env.example` 为 `.env`，填入你的 API Key（见下方申请指南）：

```bash
# ============================================================
# Zotero Brain - API 密钥配置
# ============================================================

# Zotero Web API（必填）
ZOTERO_USER_ID=你的用户ID
ZOTERO_API_KEY=你的API密钥
ZOTERO_LIBRARY_TYPE=user

# MinerU（必填 - PDF 解析）
MINERU_TOKEN=你的MinerU Token
MINERU_MODEL=vlm

# 智谱 BigModel（必填 - 文本向量化）
ZHIPU_API_KEY=你的智谱API密钥

# CORE API（可选 - Open Access PDF 搜索）
CORE_API_KEY=你的CORE API密钥
```

### 步骤 4：配置 MCP 客户端

在你的 MCP 客户端（如 WorkBuddy、Cursor）中添加 Zotero Brain：

**WorkBuddy：** 在设置 → 连接器 → MCP 中添加，command 为：
```
.venv\Scripts\python.exe mcp_server.py
```
工作目录设为 zotero-brain 项目路径。

**Cursor / 其他 MCP 客户端：** 在 `mcp.json` 中添加：
```json
{
  "mcpServers": {
    "zotero-brain": {
      "command": ".venv/Scripts/python.exe",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/zotero-brain"
    }
  }
}
```

### 步骤 5：首次批量入库

将你现有的 Zotero 文献库全部向量化：

```bash
.venv\Scripts\python.exe run_ingest.py
```

这会把所有有 PDF 的论文解析并入库。之后新增的论文只需逐个 `ingest_paper` 即可。

## API Key 申请指南

| API | 用途 | 申请地址 | 费用 |
|---|---|---|---|
| **Zotero Web API** | 读写你的文献库 | https://www.zotero.org/settings/keys | 免费 |
| **MinerU** | PDF → 结构化 Markdown | https://mineru.net （注册后获取 Token） | 有免费额度 |
| **智谱 BigModel** | 文本向量化（Embedding-3） | https://open.bigmodel.cn （注册后获取 API Key） | ¥0.0007/千 token |
| **CORE API** | Open Access PDF 搜索（可选） | https://core.ac.uk/services/api | 免费（需申请） |

> **OpenAlex 和 Unpaywall 不需要 Key**，它们是免费开放的 API。

## ⚠️ 网络配置（重要）

本项目依赖多个境外 API（MinerU、OpenAlex、Unpaywall、CrossRef、Sci-Hub），**需要能访问境外网络**。

如果你使用 TUN 模式代理（如 Clash Verge、v2rayN 等），可能会遇到 MinerU 国内 API 被代理绕路导致超时的问题。Zotero Brain 内置了 `network_helper.py`，会自动将 MinerU 的国内流量绕过 TUN 走直连。

**但你需要确保：**
1. 代理软件已开启，且支持 OpenAlex/Unpaywall/CrossRef 等境外 API
2. 如果使用 WorkBuddy，MinerU 流量会被自动绕过，其他 MCP 连接器不受影响

## 项目结构

```
zotero-brain/
├── mcp_server.py          # MCP Server（11 个工具）
├── zotero_sync.py         # Zotero Web API：文件夹、创建、元数据
├── paper_discovery.py     # 学术搜索：OpenAlex/arXiv/CrossRef/Semantic Scholar
├── paper_importer.py      # PDF 下载瀑布 + Zotero 导入 + PDF 归档
├── pdf_parser.py          # MinerU Cloud API（PDF → Markdown）
├── chunker.py             # 文本切块（按章节切 500-1500 字块）
├── embedder.py            # 智谱 Embedding-3 向量化
├── vector_store.py        # ChromaDB 向量存储 + 搜索
├── network_helper.py      # TUN 绕过（MinerU 国内流量直连）
├── config.py              # 配置加载（.env → Python）
├── run_ingest.py          # 批量入库脚本
│
├── data/
│   ├── chroma_db/         # ChromaDB 向量数据库
│   ├── papers/            # PDF 永久存储（linked_file 指向这里）
│   ├── downloads/         # PDF 临时下载目录
│   └── collection_map.json
│
├── parsed/                # MinerU 解析缓存（每篇论文一个子目录）
│   └── {key}/
│       ├── {key}.md       # 解析后的 Markdown
│       └── images/        # 论文中的图片
│
├── .env                   # API 密钥（不提交 git）
├── .gitignore
└── requirements.txt
```

## PDF 生命周期

一篇论文从下载到入库，PDF 全程只存 **1 份**：

```
download_paper(doi)
  → data/downloads/（临时文件）
      ↓
import_to_zotero()
  → 移到 data/papers/（永久存储）
  → Zotero linked_file 指向这里
      ↓
ingest_paper()
  → 从 data/papers/ 直接读取 PDF（不复制）
  → MinerU 解析结果缓存到 parsed/{key}/{key}.md + images/
      ↓
后续所有操作
  → 读 parsed/{key}/{key}.md 缓存，不碰 PDF
```

## MCP Server 工具清单（11 个）

### 搜索
| 工具 | 用途 |
|---|---|
| `search_papers` | 语义搜索文献库（支持指定文件夹、锁定特定论文） |
| `discover_papers` | 搜学术数据库（OpenAlex/arXiv/CrossRef/Semantic Scholar） |

### 下载 + 导入 + 入库
| 工具 | 用途 |
|---|---|
| `download_paper` | 6 级瀑布下载 PDF，不碰 Zotero 不碰向量库 |
| `import_to_zotero` | 导入 PDF + 元数据到 Zotero（linked_file 附件） |
| `ingest_paper` | PDF → OCR → 切块 → 向量化入库 |

### 文件夹管理
| 工具 | 用途 |
|---|---|
| `list_collections` | Zotero 文件夹 + 向量库 + 同步状态 |
| `create_collection` | 同时创建 Zotero 文件夹 + 向量库 |

### 引用
| 工具 | 用途 |
|---|---|
| `get_bibtex` | 精确引用 + 语义推荐（辅助写作） |

### 深度阅读
| 工具 | 用途 |
|---|---|
| `get_paper_chunks` | 论文结构目录（了解论文长什么样） |
| `expand_context` | 扩展上下文（深入读特定段落） |
| `read_paper_full` | 读全文 |

## 常见问题

**Q：我的论文已经有 PDF 了，还需要重新下载吗？**
不需要。`ingest_paper` 支持直接传 `pdf_path` 参数，跳过下载步骤。

**Q：向量库和 Zotero 不同步了怎么办？**
`list_collections` 会显示哪些文件夹已同步、哪些没有。用 `create_collection` 创建缺失的映射，然后对未同步的论文跑 `ingest_paper`。

**Q：MinerU 解析失败怎么办？**
检查网络是否能访问 mineru.net。如果开了 TUN 模式，`network_helper.py` 会自动处理绕过。也可以手动在 `parsed/{key}/` 下放置同名 `.md` 文件跳过解析。

**Q：我想只搜索不入库，可以吗？**
可以。`discover_papers` 只搜索不入库。`download_paper` 只下载不入库。每一步都是独立的。

**Q：支持中文论文吗？**
MinerU 支持中文 OCR，智谱 Embedding 支持中文。中文论文可以正常解析和搜索。

## 技术栈

| 组件 | 用什么 | 说明 |
|---|---|---|
| Zotero 读写 | pyzotero + Zotero Web API v3 | 免费，需 User ID + API Key |
| PDF 解析 | MinerU Cloud API（VLM 模型） | REST API，不占本地 GPU |
| 文本向量化 | 智谱 Embedding-3（2048 维） | 云端 API |
| 向量数据库 | ChromaDB（本地持久化） | 纯文件存储，无需服务器 |
| Agent 接口 | MCP（Model Context Protocol） | stdio 通信 |

## License

MIT License. 欢迎使用和贡献。
