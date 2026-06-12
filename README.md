# Zotero Brain

> Turn Zotero into a living knowledge base -- RAG pipeline + MCP Server that lets AI Agents semantically search, read, and analyze your paper library.

## What it does

Zotero is great at managing papers, but it's essentially a dead database -- you can only search by title/author/tag, not by **meaning**.

Zotero Brain fixes that:

```
Zotero  -->  MinerU Cloud API (PDF -> Markdown)  -->  Chunking  -->  ZhiPu Embedding  -->  ChromaDB
                                                                                              |
                                                                                         MCP Server (10 tools)
                                                                                              |
                                                                                    AI Agent (WorkBuddy / IDE)
```

**Key features:**
- Semantic search across your entire library ("find papers about solid electrolyte interface stability")
- Multi-paper comparison (methods, results, experimental design)
- Full-text Q&A (not just abstracts, not hallucinated)
- BibTeX citation export
- Auto-discovery from OpenAlex / arXiv / CrossRef / Semantic Scholar
- 6-level download cascade (OpenAlex OA -> Unpaywall -> CORE -> arXiv -> Sci-Hub -> manual)
- Per-Collection vector stores (batteries stay separate from biology)

## Architecture

- **No local GPU or models.** PDF parsing uses MinerU Cloud API (VLM, REST).
- **No heavy SDK.** We call MinerU via raw `httpx` REST calls -- keeps deps lean.
- **TUN mode compatible.** `network_helper.py` routes MinerU domestic traffic around TUN proxy via DoH + monkey-patching.
- **MCP Server** over stdio, exposes 10 tools to any MCP-compatible AI agent.

## Installation

```bash
git clone <repo-url>
cd zotero-brain
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

## Configuration

Create a `.env` file in the project root:

```env
# Zotero Web API (https://www.zotero.org/settings/keys)
ZOTERO_API_KEY=your_zotero_api_key
ZOTERO_USER_ID=your_zotero_user_id

# Local Zotero storage path (for PDF downloads)
ZOTERO_LOCAL_STORAGE=C:\Users\you\Zotero\storage

# MinerU Cloud API (https://mineru.net)
MINERU_TOKEN=your_mineru_token

# ZhiPu AI Embedding (https://open.bigmodel.cn)
ZHIPU_API_KEY=your_zhipu_api_key
ZHIPU_MODEL=embedding-3

# Unpaywall (https://unpaywall.org/products/related)
UNPAYWALL_EMAIL=you@example.com

# CORE API (https://core.ac.uk/services/api) -- optional
CORE_API_KEY=your_core_api_key

# OpenAlex polite pool -- optional, uses UNPAYWALL_EMAIL by default
# OPENALEX_EMAIL=you@example.com

# Semantic Scholar -- optional
SEMANTIC_SCHOLAR_API_KEY=your_key
```

## Usage

### As MCP Server (recommended)

Add to your MCP client config (e.g. WorkBuddy `mcp.json`):

```json
{
  "mcpServers": {
    "zotero-brain": {
      "command": "F:\\MyProjects\\zotero-brain\\.venv\\Scripts\\python.exe",
      "args": ["F:\\MyProjects\\zotero-brain\\mcp_server.py"]
    }
  }
}
```

Then use the 10 tools from any AI agent:

| Tool | What it does |
|------|-------------|
| `search_papers` | Semantic search in your library |
| `compare_papers` | Compare multiple papers side-by-side |
| `get_bibtex` | Generate BibTeX citations |
| `list_collections` | List all Zotero Collections |
| `ingest_paper` | Ingest a single paper (parse + embed) |
| `get_paper_chunks` | Browse paper chunk structure |
| `expand_context` | Expand context around a specific chunk |
| `read_paper_full` | Read full paper text from cache |
| `discover_papers` | Search OpenAlex / arXiv / CrossRef for new papers |
| `fetch_and_ingest` | Download + import to Zotero + parse + embed (full pipeline) |

### Batch ingest (CLI)

```bash
# Incremental (only new papers)
python run_ingest.py

# Full re-ingest
python run_ingest.py --no-incremental

# Limit to 10 papers
python run_ingest.py --limit 10

# Filter by collection name
python run_ingest.py --collection "sodium-ion"

# Custom batch size
python run_ingest.py --batch-size 10
```

### Single paper parse

```bash
python pdf_parser.py path/to/paper.pdf [item_key]
```

## Project structure

```
zotero-brain/
  mcp_server.py        # MCP Server (stdio, 10 tools)
  pdf_parser.py        # MinerU Cloud API (raw httpx)
  paper_discovery.py   # Academic DB search (OpenAlex, arXiv, CrossRef, S2)
  paper_importer.py    # 6-level download cascade + Zotero import
  chunker.py           # Markdown -> chunks
  embedder.py          # ZhiPu Embedding-3
  vector_store.py      # ChromaDB operations
  zotero_sync.py       # Zotero Web API client
  network_helper.py    # TUN bypass for MinerU (DoH + monkey-patch)
  run_ingest.py        # Batch ingest CLI
  config.py            # Config loader (.env)
  .env                 # API keys (not committed)
  data/chroma_db/      # ChromaDB vector store
  parsed/              # MinerU parsed Markdown cache
```

## Notes

- All code comments are in **English only** (Windows GBK compatibility). Chinese is only used in runtime string data (tool descriptions, log messages).
- `network_helper.py` monkey-patches `httpx` to bypass TUN proxy for MinerU domestic domains. This is transparent to the caller.
- The MinerU SDK (`pip install mineru`) is **NOT** required. We use raw REST API calls to keep dependencies lean.

## License

MIT
