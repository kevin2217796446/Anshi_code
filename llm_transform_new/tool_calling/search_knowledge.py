"""
许可证模糊搜索工具。大模型在识别许可证前调用，获取 top-5 最相似的许可证名称，提升准确率。
基于拆分表达式 + 嵌入向量 + RapidFuzz + trigram + RRF 融合。
"""

import csv
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

import threading as _thr
_load_lock = _thr.Lock()

# 数据路径
_CSV_PATH = Path(__file__).parent.parent / "数据" / "知识库数据" / "license_knowledge.csv"
_RRF_K = 10

_names: list[str] = []
_spdx_map: dict[str, str] = {}
_embeddings: np.ndarray | None = None
_split_re = re.compile(r'\band\b|\bor\b|\bwith\b|[&|;()]', re.IGNORECASE)



def _load():
    global _names, _spdx_map, _embeddings
    if _embeddings is not None:
        return
    with _load_lock:
        if _embeddings is not None:
            return
        with open(_CSV_PATH, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                _names.append(row["name"])
                _spdx_map[row["name"]] = row["spdx_name"]
        _embeddings = get_model().encode(_names, show_progress_bar=False, convert_to_numpy=True)


# 模块导入时立即异步预加载（不阻塞 import，首次调用若尚未完成会自动等待）
import threading
_load_thread: threading.Thread | None = None


def _preload_async():
    global _load_thread
    if _names:
        return
    _load_thread = threading.Thread(target=_load, daemon=True)
    _load_thread.start()


def wait_until_loaded(timeout: float = 60):
    """等待向量模型加载完成（可选，用于确保首次调用不卡顿）。"""
    if _load_thread and _load_thread.is_alive():
        print("等待向量模型加载完成...")
        _load_thread.join(timeout=timeout)


_preload_async()  # 导入时自动开始后台加载


def _split(text: str) -> list[str]:
    parts = _split_re.split(text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]


def _embed_search(query: str, limit: int = 15) -> list[dict]:
    qv = get_model().encode([query], convert_to_numpy=True)
    sims = np.dot(qv, _embeddings.T)[0]
    idx = np.argsort(sims)[::-1][:limit]
    return [{"name": _names[i], "spdx_name": _spdx_map[_names[i]], "score": round(sims[i] * 100, 1)}
            for i in idx if sims[i] > 0]


def _fuzz_search(query: str, limit: int = 15) -> list[dict]:
    results = fuzz_process.extract(query, _names, scorer=fuzz_scorer.token_sort_ratio, limit=limit)
    return [{"name": n, "spdx_name": _spdx_map[n], "score": round(s, 1)} for n, s, _ in results if s > 0]


def _trigram_jaccard(a: str, b: str) -> float:
    def tri(s):
        s = "  " + s.lower() + " "
        return {s[i:i + 3] for i in range(len(s) - 2)}
    q, c = tri(a), tri(b)
    return len(q & c) / len(q | c) * 100 if q and c else 0


def _tri_search(query: str, limit: int = 15) -> list[dict]:
    scored = [(n, _trigram_jaccard(query, n)) for n in _names]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [{"name": n, "spdx_name": _spdx_map[n], "score": round(s, 1)} for n, s in scored[:limit]]


def _fragment_merge(search_fn, fragments, limit=10):
    merged: dict[str, dict] = {}
    for frag in fragments:
        for item in search_fn(frag, limit=limit):
            n = item["name"]
            if n not in merged or item["score"] > merged[n]["score"]:
                merged[n] = item
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)


def _rrf_fuse(results_lists: list[tuple[list[dict], float]], k: int = _RRF_K, top_n: int = 5) -> list[dict]:
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}
    for lst, w in results_lists:
        for rank, item in enumerate(lst, 1):
            key = item["name"]
            scores[key] = scores.get(key, 0.0) + w / (k + rank)
            if key not in items:
                items[key] = item
    sorted_keys = sorted(scores, key=scores.get, reverse=True)
    return [{**items[k], "rrf_score": round(scores[k], 4)} for k in sorted_keys[:top_n]]


def _search(query: str, top_n: int = 5) -> list[dict]:
    """内部搜索函数。"""
    if not query.strip():
        return []
    fragments = _split(query) or [query]
    return _rrf_fuse([
        (_fragment_merge(_embed_search, fragments), 2.5),
        (_fragment_merge(_fuzz_search, fragments), 1.0),
        (_fragment_merge(_tri_search, fragments), 1.0),
    ], top_n=top_n)


@tool
def search_similar_licenses(license_text: str) -> str:
    """
    在 SPDX + ScanCode 许可证知识库中搜索与输入文本最相似的前 5 个许可证。

    使用时机：大模型在开始识别许可证之前，必须先调用本工具。
    本工具会返回知识库中匹配度最高的许可证名称及 SPDX 标识符，作为参考上下文，
    帮助大模型输出更准确的识别结果。

    参数:
    - license_text: 待识别的原始许可证文本

    返回:
    - JSON 格式的 top-5 相似许可证列表，每项含 name, spdx_name, rrf_score
    """
    _load()
    results = _search(license_text)
    # 格式化为映射关系展示
    lines = []
    for i, r in enumerate(results, 1):
        name = r["name"]
        spdx = r["spdx_name"]
        rrf = round(float(r["rrf_score"]), 4) if isinstance(r["rrf_score"], (np.floating, np.integer)) else r["rrf_score"]
        lines.append(f"{i}. {name} --> {spdx}  (rrf_score={rrf})")
    return json.dumps({"query": license_text, "results": lines}, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    print(search_similar_licenses.invoke("NVIDIA"))