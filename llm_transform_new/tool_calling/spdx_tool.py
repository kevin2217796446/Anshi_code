"""
SPDX 标识符校验工具。大模型输出后，对每个不带 LicenseRef-cleansource- 前缀的字段，
强制查询 SPDX 数据库，不存在的字段不允许输出。
"""

import json
import re
from pathlib import Path
from langchain_core.tools import tool

_SPDX_IDS: set[str] = set()
_DATA_PATH = Path(__file__).parent.parent / "数据" / "知识库数据" / "spdx_id.json"


def _load_spdx_ids():
    global _SPDX_IDS
    if not _SPDX_IDS:
        _SPDX_IDS = set(json.loads(_DATA_PATH.read_text(encoding="utf-8")))


@tool
def validate_spdx_identifier(expression: str) -> str:
    """
    校验许可证表达式中的 SPDX 标识符是否存在于官方 SPDX 数据库中。

    使用时机：大模型输出 license_spdx_name 后，必须调用本工具验证。
    对表达式中用 & 或 | 分割的每个字段：
    - 若字段不带 LicenseRef-cleansource- 前缀 → 必须在 SPDX 数据库中
    - 若不在数据库中 → 返回失败，大模型必须重新思考答案

    参数:
    - expression: 完整的许可证表达式，如 "MIT&Apache-2.0|GPL-3.0-only"

    返回:
    - "PASS" 或 "FAIL: <不存在的字段列表>"
    """
    _load_spdx_ids()

    fields = re.split(r'[&|]', expression)
    invalid = []

    for field in fields:
        field = field.strip()
        if not field:
            continue
        if field.startswith("LicenseRef-cleansource-"):
            continue  # 带前缀的字段由 scancode_tool 校验
        if field == "none" or field == "Commercial":
            continue  # 特殊值放行
        if field not in _SPDX_IDS:
            invalid.append(field)

    if invalid:
        return f"FAIL: 以下标识符不在 SPDX 数据库中: {', '.join(invalid)}。请重新思考，改用正确的 SPDX 标识符。"
    return "PASS"
