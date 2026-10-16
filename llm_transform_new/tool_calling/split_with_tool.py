"""WITH 表达式拆分工具。将 classify 返回 2/3 的候选按 WITH 拆分为左右两部分。"""

import re
from langchain_core.tools import tool

# 匹配各种 WITH 变体：WITH / with / With / with- / WITH- 等，两边可能带空格
_WITH_RE = re.compile(r'\s*-?\s*(?:with|WITH)\s*-?\s*', re.IGNORECASE)


@tool
def split_with_expression(license_text: str) -> str:
    """
    将包含 WITH 的许可证表达式拆分为左边（主体）和右边（exception）两部分。

    使用时机：当 classify_license_type 返回 2 或 3 时，必须调用本工具拆分。
    本工具支持各种 WITH 变体（大小写、带横杠、带空格等均会正确拆分）。

    参数:
    - license_text: 含 WITH 的许可证表达式，如 "GPL-2.0-only WITH Classpath-exception-2.0"

    返回:
    - 格式为 "左边：{左边值}  右边：{右边值}" 的字符串
    """
    parts = _WITH_RE.split(license_text, maxsplit=1)
    if len(parts) < 2:
        return f"左边：{license_text.strip()}  右边：（无法拆分）"

    left = parts[0].strip()
    right = parts[1].strip()
    return f"左边：{left}  右边：{right}"
