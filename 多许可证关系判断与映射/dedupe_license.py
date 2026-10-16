"""清除 license 字段里「同一括号级别」内重复的许可证值。

规则：
1. 同一级的 | 或 & 两侧有相同值 -> 去重（保留第一次出现）。
2. 既有 | 又有 & 时，以第一个关系符号为准：重复值连同它前面的那个关系符号一并删除。
3. 不同括号级别之间不去重，只在同一个括号级别内去重。

例：
  GPL-2.0-only|GPL-2.0-only|Apache-2.0       -> GPL-2.0-only|Apache-2.0
  GPL-2.0-only|Apache-2.0|GPL-2.0-only       -> GPL-2.0-only|Apache-2.0
  GPL-2.0-only|Apache-2.0&GPL-2.0-only       -> GPL-2.0-only|Apache-2.0
  GPL-2.0-only|(Apache-2.0|GPL-2.0-only)     -> 不去重（跨级别）
"""

import argparse
import io
import json
import os
from pathlib import Path

# ──── 全局配置（按需修改） ────
BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "数据" / "maven_multi_license_purl_version" / "8_21" / "full_data.json"
OUTPUT_FILE = BASE_DIR / "数据" / "maven_multi_license_purl_version" / "8_21" / "full_data_dedupe.json"  # 原地写回；如需保留原文件，改成别的路径即可

OPS = "|&"


def _is_group_paren(s: str, open_idx: int) -> bool:
    """判断 open_idx 处的 '(' 是否为分组括号（其内部含 | 或 &）。

    用于区分真正的分组 '(A|B)' 与许可证名称里字面的括号，如 '(GFTC)'。
    """
    depth = 1
    for i in range(open_idx + 1, len(s)):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return False
        elif c in OPS:
            return True
    return False


def _parse_level(s: str, pos: int):
    """解析一层表达式：term (op term)*，遇到 ')' 或结尾停止。

    返回 (terms, ops, pos)。分组 term 会递归解析（内部去重后拼接）。
    """
    terms: list[str] = []
    ops: list[str] = []
    n = len(s)
    while True:
        # 跳过空白
        while pos < n and s[pos].isspace():
            pos += 1
        if pos >= n or s[pos] == ")":
            break

        # ── 解析一个 term ──
        if s[pos] == "(" and _is_group_paren(s, pos):
            # 分组：递归解析内部，再去掉右括号
            inner_terms, inner_ops, pos = _parse_level(s, pos + 1)
            while pos < n and s[pos].isspace():
                pos += 1
            if pos < n and s[pos] == ")":
                pos += 1
            terms.append("(" + _rebuild(inner_terms, inner_ops) + ")")
        else:
            # 普通名称；可能含字面的成对括号
            chars: list[str] = []
            literal_depth = 0
            while pos < n:
                c = s[pos]
                if c in OPS and literal_depth == 0:
                    break
                if c == "(":
                    if literal_depth == 0 and _is_group_paren(s, pos):
                        break
                    literal_depth += 1
                elif c == ")":
                    if literal_depth == 0:
                        break  # 本层结束
                    literal_depth -= 1
                chars.append(c)
                pos += 1
            name = "".join(chars).strip()
            if name:
                terms.append(name)

        # ── 解析操作符 ──
        while pos < n and s[pos].isspace():
            pos += 1
        if pos < n and s[pos] in OPS:
            ops.append(s[pos])
            pos += 1
        else:
            break
    return terms, ops, pos


def _rebuild(terms: list[str], ops: list[str]) -> str:
    if not terms:
        return ""
    out = terms[0]
    for op, term in zip(ops, terms[1:]):
        out += op + term
    return out


def dedupe_expr(s: str) -> str:
    """对同一括号级别去重，返回新表达式字符串。"""
    if not isinstance(s, str):
        return s
    s = s.strip()
    if not s:
        return s
    terms, ops, _ = _parse_level(s, 0)
    seen: set[str] = set()
    kept_terms: list[str] = []
    kept_ops: list[str] = []
    for idx, term in enumerate(terms):
        if term in seen:
            continue  # 丢弃重复 term 及其前面的操作符
        seen.add(term)
        kept_terms.append(term)
        if idx > 0:
            kept_ops.append(ops[idx - 1])
    return _rebuild(kept_terms, kept_ops)


def main() -> None:
    ap = argparse.ArgumentParser(description="清除 license 字段里同一括号级别内重复的许可证值")
    ap.add_argument("input", type=Path, nargs="?", default=INPUT_FILE, help="输入 JSON 文件")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="输出 JSON 文件（默认 <input>_dedupe.json）")
    args = ap.parse_args()

    inp = Path(args.input)
    outp = args.output or inp.with_name(inp.stem + "_dedupe.json")

    with io.open(inp, encoding="utf-8") as f:
        data = json.load(f)

    changed = 0
    for rec in data:
        old = rec.get("license")
        new = dedupe_expr(old)
        if new != old:
            rec["license"] = new
            changed += 1
            if changed <= 10:
                print(f"  {old!r}  ->  {new!r}")

    # 先写临时文件再替换，避免中途出错损坏原文件
    tmp = outp.with_suffix(outp.suffix + ".tmp")
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, outp)

    print(f"总记录: {len(data)}，修改了 {changed} 条的 license 字段")
    print(f"已写回: {outp}")


if __name__ == "__main__":
    main()
