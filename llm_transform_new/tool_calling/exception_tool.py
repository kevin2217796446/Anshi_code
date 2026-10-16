"""Exception 标识符校验工具。校验 WITH-exception 字段是否存在于 exception_id.json。"""

import json
import re
from pathlib import Path
from langchain_core.tools import tool

_EXCEPTION_IDS: set[str] = set()
_DATA_PATH = Path(__file__).parent.parent / "数据" / "知识库数据" / "exception_id.json"


def _load_exception_ids():
    global _EXCEPTION_IDS
    if not _EXCEPTION_IDS:
        _EXCEPTION_IDS = set(json.loads(_DATA_PATH.read_text(encoding="utf-8")))


@tool
def validate_exception_identifier(expression: str) -> str:
    """
    校验许可证表达式中的 exception 标识符是否存在于 exception_id.json 中。

    使用时机：大模型输出含 -with- 或 with...exception 的标识符后，必须调用本工具验证。
    若标识符不在 exception 数据库中 → 返回失败，大模型必须重新思考答案。

    参数:
    - expression: 许可证表达式

    返回:
    - "PASS" 或 "FAIL: <不存在的字段列表>"
    """
    _load_exception_ids()
    fields = re.split(r'[&|]', expression)
    invalid = []
    for field in fields:
        field = field.strip()
        if not field:
            continue
        if field in _EXCEPTION_IDS:
            continue  # 在库中，通过
        if field == "none" or field == "Commercial":
            continue  # 特殊值放行
        invalid.append(field)

    if invalid:
        return f"FAIL: 以下 exception 标识符不在数据库中: {', '.join(invalid)}。请重新思考。"
    return "PASS"
