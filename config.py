# -*- coding: utf-8 -*-
"""Zotero Brain 配置"""
import os
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# -- .env loading --
def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()
def _e(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

# -- API Keys --
ZOTERO_USER_ID = _e("ZOTERO_USER_ID")
ZOTERO_API_KEY = _e("ZOTERO_API_KEY")
ZOTERO_LIBRARY_TYPE = _e("ZOTERO_LIBRARY_TYPE", "user")  # "user" or "group"
DEFAULT_COLLECTION = "uncategorized"  # Papers not belonging to any Collection
MINERU_TOKEN = _e("MINERU_TOKEN")
MINERU_MODEL = "vlm"
ZHIPU_API_KEY = _e("ZHIPU_API_KEY")
UNPAYWALL_EMAIL = _e("UNPAYWALL_EMAIL", "zoterobrain@gmail.com")
OPENALEX_EMAIL = _e("OPENALEX_EMAIL", UNPAYWALL_EMAIL)  # polite pool, faster responses
CORE_API_KEY = _e("CORE_API_KEY", "")  # https://core.ac.uk free to apply

# -- Paths --
PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"
CHROMA_DIR = DATA_DIR / "chroma_db"
PARSED_DIR = PROJECT_DIR / "parsed"
PAPERS_DIR = DATA_DIR / "papers"          # 永久 PDF 存储（linked_file 指向这里）
ZOTERO_LOCAL_STORAGE = Path(os.path.expanduser(r"~\Zotero\storage"))
for _d in [CHROMA_DIR, PARSED_DIR, DATA_DIR, PAPERS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# -- ZHIPU Embedding-3 (official docs: docs.bigmodel.cn) --
# input supports string or string[], max 64 items per request, single item <=3072 tokens
ZHIPU_EMBED_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
ZHIPU_MODEL = "embedding-3"
ZHIPU_DIM = 2048
ZHIPU_MAX_BATCH = 64       # Official limit: 64 items
ZHIPU_MAX_CHARS = 6000     # Safe truncation for ~3072 tokens

# -- Collection name mapping --
# ChromaDB naming rules: 3-512 chars, [a-z0-9._-], must start/end with a-z0-9
# Chinese name -> kebab-case English (Chinese name stored in metadata as display_name)
# Phase 4: DeepSeek 已砍掉，映射由 Agent 通过 create_collection 工具写入
_NAME_MAP_FILE = DATA_DIR / "collection_map.json"

def _load_name_map() -> dict:
    import re as _re
    if _NAME_MAP_FILE.exists():
        raw = json.loads(_NAME_MAP_FILE.read_text("utf-8"))
        result = {}
        for cn, val in raw.items():
            if isinstance(val, str):
                is_auto = bool(_re.match(r'^col-[a-f0-9]{10}$', val))
                result[cn] = {"slug": val, "auto": is_auto}
            elif isinstance(val, dict):
                result[cn] = {"slug": val.get("slug", ""), "auto": val.get("auto", False)}
        return result
    return {}

def _save_name_map(m: dict):
    _NAME_MAP_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

_NAME_MAP = _load_name_map()
_name_lock = threading.Lock()

def _get_slug(chinese_name: str) -> str | None:
    entry = _NAME_MAP.get(chinese_name)
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    return entry.get("slug")

def _has_auto_slug(chinese_name: str) -> bool:
    entry = _NAME_MAP.get(chinese_name)
    if entry is None:
        return False
    if isinstance(entry, str):
        return entry.startswith("col-")
    return entry.get("auto", False)

def translate_collection_name(chinese_name: str) -> str:
    """中文 Collection 名 → ChromaDB 安全名（纯查映射表，不再调 DeepSeek）"""
    if not chinese_name or chinese_name == "uncategorized":
        return "uncategorized"
    with _name_lock:
        slug = _get_slug(chinese_name)
        if slug is not None:
            return slug
    return ensure_collection_mapping(chinese_name)

def register_collection_mapping(chinese_name: str, chroma_name: str, auto: bool = False):
    """注册中文名 → ChromaDB 英文名的映射（由 create_collection 工具调用）"""
    import re
    if not re.match(r'^[a-z0-9][a-z0-9._-]{1,510}[a-z0-9]$', chroma_name):
        raise ValueError(
            f"ChromaDB 名称 '{chroma_name}' 不合法。"
            f"要求: 3-512 字符, [a-z0-9._-], 首尾必须 a-z0-9"
        )
    with _name_lock:
        _NAME_MAP[chinese_name] = {"slug": chroma_name, "auto": auto}
        _save_name_map(_NAME_MAP)
    label = "auto-generated" if auto else "registered"
    logger.info(f"Collection 映射已{label}: '{chinese_name}' → '{chroma_name}'")

def ensure_collection_mapping(col_name: str) -> str:
    """确保 Collection 有中英文映射。已有则直接返回 slug，缺失则自动生成并注册。

    返回: ChromaDB 安全名 (slug)
    """
    if col_name == DEFAULT_COLLECTION:
        return "uncategorized"
    with _name_lock:
        slug = _get_slug(col_name)
        if slug is not None:
            return slug
    import hashlib
    h = hashlib.md5(col_name.encode()).hexdigest()[:10]
    slug = f"col-{h}"
    register_collection_mapping(col_name, slug, auto=True)
    logger.warning(
        f"⚠ 自动生成 Collection slug: '{col_name}' → '{slug}'。"
        f"建议通过 create_collection 工具设置规范英文名。"
    )
    return slug

def get_display_name(chroma_name: str) -> str:
    """ChromaDB 名 → 中文显示名（反向查找）"""
    with _name_lock:
        for zh, entry in _NAME_MAP.items():
            en = entry if isinstance(entry, str) else entry.get("slug", "")
            if en == chroma_name:
                return zh
    return chroma_name


def get_name_map_snapshot() -> dict:
    """返回映射表的只读快照（线程安全）。用于 list_collections 等需要遍历的场景。"""
    with _name_lock:
        return {
            cn: entry if isinstance(entry, dict) else {"slug": entry, "auto": True}
            for cn, entry in _NAME_MAP.items()
        }
