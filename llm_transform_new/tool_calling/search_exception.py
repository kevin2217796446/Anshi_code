"""Exception 标识符模糊搜索工具：在 exception_id.json 中搜索 top-5 最相似的 ID。"""

import json
import re
from pathlib import Path

import numpy as np
from langchain_core.tools import tool
from rapidfuzz import process as fuzz_process, fuzz as fuzz_scorer

try:
    from ._embed_model import get_model
except ImportError:
    from _embed_model import get_model

_DATA = Path(__file__).parent.parent / "数据" / "知识库数据" / "exception_id.json"
_RRF_K = 10

_ids: list[str] = []
_embeddings: np.ndarray | None = None
_split_re = re.compile(r'\band\b|\bor\b|\bwith\b|[&|;()]', re.IGNORECASE)

import threading as _thr
_load_lock = _thr.Lock()


def _load():
    global _ids, _embeddings
    if _embeddings is not None:
        return
    with _load_lock:
        if _embeddings is not None:
            return
        _ids = json.loads(_DATA.read_text(encoding="utf-8"))
        _embeddings = get_model().encode(_ids, show_progress_bar=False, convert_to_numpy=True)


def _split(text: str) -> list[str]:
    parts = _split_re.split(text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]


def _embed_search(query: str, limit: int = 15) -> list[dict]:
    qv = get_model().encode([query], convert_to_numpy=True)
    sims = np.dot(qv, _embeddings.T)[0]
    idx = np.argsort(sims)[::-1][:limit]
    return [{"exception_id": _ids[i], "score": round(float(sims[i]) * 100, 1)} for i in idx if sims[i] > 0]


def _fuzz_search(query: str, limit: int = 15) -> list[dict]:
    results = fuzz_process.extract(query, _ids, scorer=fuzz_scorer.token_sort_ratio, limit=limit)
    return [{"exception_id": i, "score": round(s, 1)} for i, s, _ in results if s > 0]


def _trigram_jaccard(a: str, b: str) -> float:
    def tri(s):
        s = "  " + s.lower() + " "
        return {s[i:i + 3] for i in range(len(s) - 2)}
    q, c = tri(a), tri(b)
    return len(q & c) / len(q | c) * 100 if q and c else 0


def _tri_search(query: str, limit: int = 15) -> list[dict]:
    scored = [(i, _trigram_jaccard(query, i)) for i in _ids]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [{"exception_id": i, "score": round(s, 1)} for i, s in scored[:limit]]


def _fragment_merge(search_fn, fragments, limit=10):
    merged: dict[str, dict] = {}
    for frag in fragments:
        for item in search_fn(frag, limit=limit):
            k = item["exception_id"]
            if k not in merged or item["score"] > merged[k]["score"]:
                merged[k] = item
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)


def _rrf_fuse(results_lists: list[tuple[list[dict], float]], k: int = _RRF_K, top_n: int = 5) -> list[dict]:
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    for lst, w in results_lists:
        for rank, item in enumerate(lst, 1):
            key = item["exception_id"]
            scores[key] = scores.get(key, 0.0) + w / (k + rank)
            if key not in items:
                items[key] = item
    sorted_keys = sorted(scores, key=scores.get, reverse=True)
    return [{**items[k], "rrf_score": round(scores[k], 4)} for k in sorted_keys[:top_n]]


def _search(query: str, top_n: int = 5) -> list[dict]:
    if not query.strip():
        return []
    fragments = _split(query) or [query]
    return _rrf_fuse([
        (_fragment_merge(_embed_search, fragments), 2.5),
        (_fragment_merge(_fuzz_search, fragments), 1.0),
        (_fragment_merge(_tri_search, fragments), 1.0),
    ], top_n=top_n)


@tool
def search_exception(license_text: str) -> str:
    """
    在 SPDX 官方 Exception 数据库（86 条）中搜索与输入最相似的前 5 个 exception ID。

    使用时机：大模型遇到 "WITH xxx-exception" 模式、需要确认 exception 标识符写法时调用。
    返回的 ID 可作为输出的参考格式。

    参数:
    - license_text: 待搜索的 exception 名称或标识符

    返回:
    - JSON 格式的 top-5 相似 exception ID 列表，每项含 exception_id, rrf_score
    """
    _load()
    results = _search(license_text)
    clean = [{k: float(v) if isinstance(v, (np.floating, np.integer)) else v
              for k, v in r.items()} for r in results]
    return json.dumps(clean, ensure_ascii=False, indent=2)
