"""前缀/特殊字符判断工具。供大模型在识别许可证时判断输入类型。"""

import re
import json
from langchain_core.tools import tool

_SCANCODE_PREFIXES = ("LicenseRef-cleansource-", "LicenseRef-scancode-")
_WITH_EXCEPTION_RE = re.compile(r'with.+exception', re.IGNORECASE)


@tool
def classify_license_type(text: str) -> str:
    """
    判断输入文本的许可证类型。

    规则:
    - 包含 "with...exception" 字样 → 返回 "2"（WITH-exception 类型）
    - 以 LicenseRef-cleansource- 或 LicenseRef-scancode- 开头 → 返回 "1"（ScanCode 私有类型）
    - 以上均不满足 → 返回 "0"（SPDX 标准类型或未知）

    使用时机：LLM 在开始处理一个许可证片段前，调用本工具判断其类型，
    以决定后续调用哪个搜索/校验工具。

    参数:
    - text: 待判断的许可证文本（可以是纯文本或 JSON 字符串）

    返回:
    - "0" / "1" / "2" / "3"
    """
    # 尝试解析 JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            text = obj.get("result", obj.get("license_spdx_name", text))
    except (json.JSONDecodeError, TypeError):
        pass

    has_exception = bool(_WITH_EXCEPTION_RE.search(text))
    has_prefix = text.strip().startswith(_SCANCODE_PREFIXES)

    # with-exception 且带前缀 → 2: ScanCode + WITH Exception (4.5)
    if has_exception and has_prefix:
        return "3"

    # with-exception 且无前缀 → 3: SPDX + WITH Exception (4.4)
    if has_exception:
        return "2"

    # 有前缀无 exception → 1: ScanCode 私有 (4.3)
    if has_prefix:
        return "1"

    # 其他 → 0: SPDX 标准 (4.2)
    return "0"
