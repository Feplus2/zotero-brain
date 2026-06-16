# -*- coding: utf-8 -*-
"""
Embedder — 智谱 Embedding-3 API
官方文档: https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3

- endpoint: POST https://open.bigmodel.cn/api/paas/v4/embeddings
- input: string | string[], 最多 64 条, 单条 ≤3072 tokens
- 响应: data[].embedding (2048 维)
- 错误码 1210: 参数有误
"""
import time, logging, httpx, config

logger = logging.getLogger(__name__)

# NOTE: Do NOT clear global proxy env vars!
# Only use proxy=None + trust_env=False on this client to ensure direct connect to Zhipu API.
# Global os.environ.pop would pollute httpx clients in other modules like paper_discovery.
_client = httpx.Client(timeout=60, follow_redirects=True, proxy=None, trust_env=False)

def _post(headers, json, retries=3):
    for i in range(1, retries + 1):
        try:
            return _client.post(config.ZHIPU_EMBED_URL, headers=headers, json=json)
        except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError,
                httpx.ReadTimeout, httpx.WriteError, httpx.PoolTimeout) as e:
            if i == retries:
                raise
            time.sleep(2 ** i)

def _embed_one(text: str) -> list[float]:
    """单条向量化"""
    h = {"Authorization": f"Bearer {config.ZHIPU_API_KEY}", "Content-Type": "application/json"}
    body = {"model": config.ZHIPU_MODEL, "input": [text[:config.ZHIPU_MAX_CHARS]]}
    resp = _post(h, body)
    resp.raise_for_status()
    data = resp.json()
    if "data" not in data or not data["data"]:
        raise RuntimeError(f"Embedding failed: {data}")
    return data["data"][0]["embedding"]

def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """批量向量化（每批最多 64 条，截断到 6000 chars）"""
    results = []
    for start in range(0, len(texts), config.ZHIPU_MAX_BATCH):
        batch = texts[start:start + config.ZHIPU_MAX_BATCH]
        h = {"Authorization": f"Bearer {config.ZHIPU_API_KEY}", "Content-Type": "application/json"}
        body = {"model": config.ZHIPU_MODEL, "input": [t[:config.ZHIPU_MAX_CHARS] for t in batch]}
        try:
            resp = _post(h, body)
            resp.raise_for_status()
            data = resp.json()
            if "data" not in data:
                raise RuntimeError(f"Embedding failed: {data}")
            # Sort by index to ensure correct order
            items = sorted(data["data"], key=lambda x: x.get("index", 0))
            results.extend(item["embedding"] for item in items)
            logger.info(f"  已向量化 {len(results)}/{len(texts)}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                # Some item triggered safety filter, retry one by one
                logger.warning(f"  批次 400 错误，逐条重试...")
                for t in batch:
                    try:
                        results.append(_embed_one(t))
                    except Exception:
                        logger.warning(f"  跳过 1 条 (embedding 失败): {t[:120]}")
                        results.append(None)
                    time.sleep(0.1)
                continue
            raise
        time.sleep(0.3)  # 限流
    return results
