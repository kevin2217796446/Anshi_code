"""共享的向量嵌入模型，所有搜索工具复用同一个实例。"""

import threading
from sentence_transformers import SentenceTransformer

_MODEL_NAME = "all-MiniLM-L6-v2"
_model: SentenceTransformer | None = None
_lock = threading.Lock()


def get_model() -> SentenceTransformer:
    """获取全局唯一的 SentenceTransformer 实例。"""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                _model = SentenceTransformer(_MODEL_NAME)
    return _model
