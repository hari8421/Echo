"""Optional embedding support for semantic document search.

Works against Ollama's local embedding endpoints or an OpenAI-compatible
``/embeddings`` endpoint. Everything degrades gracefully to keyword search.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import List, Optional

from .config import Config

TIMEOUT = 120


def _post_json(url: str, payload: dict, headers: Optional[dict] = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def embed_texts(cfg: Config, texts: List[str]) -> Optional[List[List[float]]]:
    """Return one vector per input, or None when embeddings are unavailable."""
    texts = [t for t in texts if t and t.strip()]
    if not texts or not cfg.embed_model:
        return None
    try:
        if cfg.provider == "ollama":
            return _ollama_embed(cfg, texts)
        return _openai_embed(cfg, texts)
    except Exception:
        return None


def _ollama_embed(cfg: Config, texts: List[str]) -> List[List[float]]:
    base = cfg.base_url.rstrip("/")
    try:
        data = _post_json(
            base + "/api/embed", {"model": cfg.embed_model, "input": texts}
        )
        embeddings = data.get("embeddings")
        if embeddings and len(embeddings) == len(texts):
            return [[float(v) for v in vector] for vector in embeddings]
    except Exception:
        pass

    # Older Ollama versions only offer /api/embeddings (one prompt at a time).
    vectors: List[List[float]] = []
    for text in texts:
        data = _post_json(
            base + "/api/embeddings", {"model": cfg.embed_model, "prompt": text}
        )
        vector = data.get("embedding")
        if not vector:
            raise RuntimeError("No embedding returned")
        vectors.append([float(v) for v in vector])
    return vectors


def _openai_embed(cfg: Config, texts: List[str]) -> List[List[float]]:
    headers = {}
    if cfg.api_key:
        headers["Authorization"] = "Bearer " + cfg.api_key
    data = _post_json(
        cfg.base_url.rstrip("/") + "/embeddings",
        {"model": cfg.embed_model, "input": texts},
        headers=headers,
    )
    rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
    vectors = [[float(v) for v in row["embedding"]] for row in rows]
    if len(vectors) != len(texts):
        raise RuntimeError("Unexpected embedding count")
    return vectors


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)
