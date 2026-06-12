# -*- coding: utf-8 -*-
"""Zotero Brain 配置"""
import os
import json
import logging
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
DEEPSEEK_API_KEY = _e("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _e("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = "deepseek-v4-pro"  # Official docs: https://api-docs.deepseek.com
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
ZOTERO_LOCAL_STORAGE = Path(os.path.expanduser(r"~\Zotero\storage"))
for _d in [CHROMA_DIR, PARSED_DIR, DATA_DIR]:
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
# Automatically loads cache at runtime, translated by DeepSeek V4 Flash on first access
_NAME_MAP_FILE = DATA_DIR / "collection_map.json"

def _load_name_map() -> dict:
    if _NAME_MAP_FILE.exists():
        return json.loads(_NAME_MAP_FILE.read_text("utf-8"))
    return {}

def _save_name_map(m: dict):
    _NAME_MAP_FILE.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

_NAME_MAP = _load_name_map()

def translate_collection_name(chinese_name: str) -> str:
    """中文 Collection 名 → ChromaDB 安全名（DeepSeek V4 Flash 翻译）"""
    if not chinese_name or chinese_name == "uncategorized":
        return "uncategorized"
    if chinese_name in _NAME_MAP:
        return _NAME_MAP[chinese_name]

    import httpx
    prompt = (
        f"Translate this Chinese phrase to a short English kebab-case identifier "
        f"(lowercase, hyphens, max 40 chars, no spaces). Only output the identifier.\n\n"
        f"Input: {chinese_name}"
    )
    try:
        resp = httpx.Client(proxy=None, trust_env=False, timeout=30).post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-v4-flash",  # Cheap and fast, suitable for translation
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,  # V4 Flash is a reasoning model; reasoning + output need sufficient budget
                "temperature": 0,
            },
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip().strip('"').lower()
        # Clean illegal characters
        import re
        result = re.sub(r"[^a-z0-9._-]", "-", result)
        result = re.sub(r"-+", "-", result).strip("-")
        if len(result) < 3:
            result = f"col-{result}"
        _NAME_MAP[chinese_name] = result
        _save_name_map(_NAME_MAP)
        logger.info(f"Collection 翻译: '{chinese_name}' → '{result}'")
        return result
    except Exception as e:
        logger.warning(f"翻译失败 ({chinese_name}): {e}, 使用 hash")
        import hashlib
        h = hashlib.md5(chinese_name.encode()).hexdigest()[:10]
        result = f"col-{h}"
        _NAME_MAP[chinese_name] = result
        _save_name_map(_NAME_MAP)
        return result

def get_display_name(chroma_name: str) -> str:
    """ChromaDB 名 → 中文显示名（反向查找）"""
    for zh, en in _NAME_MAP.items():
        if en == chroma_name:
            return zh
    return chroma_name
