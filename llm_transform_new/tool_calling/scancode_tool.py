"""
ScanCode 标识符校验工具。大模型输出后，对每个带 LicenseRef-cleansource- 前缀的字段，
强制查询 ScanCode 数据库，不存在的字段不允许输出。
"""

import json
import re
from pathlib import Path
from langchain_core.tools import tool

_SCANCODE_IDS: set[str] = set()
_DATA_PATH = Path(__file__).parent.parent / "数据" / "知识库数据" / "scancode_id.json"


def _load_scancode_ids():
    global _SCANCODE_IDS
    if not _SCANCODE_IDS:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        # scancode_id.json 是 list of strings
        _SCANCODE_IDS = set(raw)


@tool
def validate_scancode_identifier(expression: str) -> str:
    """
    校验许可证表达式中带 LicenseRef-cleansource- 前缀的标识符是否存在于 ScanCode 数据库中。

    使用时机：大模型输出 license_spdx_name 后，必须调用本工具验证。
    对表达式中用 & 或 | 分割的每个字段：
    - 若字段带 LicenseRef-cleansource- 前缀 → 去掉前缀后必须在 ScanCode 数据库中
    - 若不在数据库中 → 返回失败，大模型必须重新思考答案

    参数:
    - expression: 完整的许可证表达式，如 "LicenseRef-cleansource-xxx&MIT"

    返回:
    - "PASS" 或 "FAIL: <不存在的字段列表>"
    """
    _load_scancode_ids()
    PREFIX = "LicenseRef-cleansource-"

    fields = re.split(r'[&|]', expression)
    invalid = []

    for field in fields:
        field = field.strip()
        if not field or not field.startswith(PREFIX):
            continue
        key = field[len(PREFIX):]
        scancode_key = f"LicenseRef-scancode-{key}"
        # 检查是否在 scancode 数据库中
        if key not in _SCANCODE_IDS and scancode_key not in _SCANCODE_IDS:
            invalid.append(field)

    if invalid:
        return f"FAIL: 以下标识符不在 ScanCode 数据库中: {', '.join(invalid)}。请重新思考，改用正确的标识符或确认该许可证是否真实存在。"
    return "PASS"
