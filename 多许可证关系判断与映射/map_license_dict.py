"""将 license / license_text 字段按 dict_correct_new.txt 映射为标准化许可证标识。

从输入 JSON 全量映射，同时合并补充映射 dict_correct_new_supplement.txt（补充可覆盖 base）。

输出:
- 映射结果:        <输入>_mapped.json          完全映射的记录（license 已全部映射为标准标识，其余字段原样保留）
- 未映射的许可证原文: <输入>_unmapped_licenses.json  含未映射许可证的原文记录（格式同 mapped，license 为映射前原文）
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK，遇到特殊字符会崩，统一转 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).parent
DATA_DIR = BASE / "数据" / "maven_multi_license_purl_version"/"8_20"
INPUT = DATA_DIR / "multi_license_full_except_empty.json"
DICT_FILE = BASE / "数据" / "原始数据" / "dict_correct_new.txt"
DICT_SUPPLEMENT = BASE / "数据" / "原始数据" / "dict_correct_new_supplement.txt"
OUTPUT_MAPPED = INPUT.with_name(INPUT.stem + "_mapped.json")
OUTPUT_PENDING = INPUT.with_name(INPUT.stem + "_unmapped_licenses.json")


def load_mapping(path: Path, skip_comments: bool = False) -> dict[str, str]:
    """解析映射文件（tab 分隔，兼容末尾空格分隔）。

    skip_comments=True 时跳过 # 注释行（仅用于补充映射文件；
    基础映射 dict_correct_new.txt 里 # 是合法的许可证文本，不能跳过）。
    """
    if not path.exists():
        return {}
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if skip_comments and line.startswith("#"):
            continue
        if "\t" in line:
            k, v = line.split("\t", 1)
            mapping[k.strip()] = v.strip()
        else:
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 2:
                mapping[parts[0].strip()] = parts[-1].strip()
    return mapping


def _license_text_of(record: dict) -> str:
    """从记录中取待映射文本：优先 license_text，回退 license。

    兼容两种字段命名：有的数据叫 license_text，有的叫 license。
    """
    for field in ("license_text", "license"):
        val = record.get(field)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


# 组合表达式里的操作符：| &，用于拆分并保留映射前后的 & / | 关系
_OP_RE = re.compile(r"\s*([|&])\s*")


def _normalize(text: str) -> str:
    """折叠换行/连续空白，并去掉 | & 两侧的多余空格（不碰括号）。

    例：'UPL-1.0\\n|Apache-2.0\\n\\n' -> 'UPL-1.0|Apache-2.0'
    """
    text = re.sub(r"\s+", " ", text).strip()
    return _OP_RE.sub(r"\1", text)


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
        elif c in "|&":
            return True
    return False


def _split_tokens(norm: str) -> list[str]:
    """把含 |/& 的表达式拆成 token：单许可证名、|、&、分组括号。

    字面的成对括号（如 (GFTC)）并入许可证名，不拆分。
    """
    tokens: list[str] = []
    i, n = 0, len(norm)
    while i < n:
        c = norm[i]
        if c in "|&":
            tokens.append(c)
            i += 1
        elif c == "(" and _is_group_paren(norm, i):
            tokens.append("(")
            i += 1
        elif c == ")":
            tokens.append(")")
            i += 1
        else:
            start = i
            depth = 0
            while i < n:
                c = norm[i]
                if c in "|&" and depth == 0:
                    break
                if c == "(":
                    if depth == 0 and _is_group_paren(norm, i):
                        break
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        break
                    depth -= 1
                i += 1
            name = norm[start:i].strip()
            if name:
                tokens.append(name)
    return tokens


def _map_text(text: str, mapping: dict[str, str], pending: set | None = None) -> str:
    """映射单个 license_text。

    多许可证（含 | / &）：
    1. 先整串带着关系符号精确匹配（含映射表里的复合 key，如 'MIT|SIL Open Font License'）；
    2. 未命中则按 | / & 拆成单个许可证逐个映射，最后加回 | / &；
       映射不到的单许可证保留原文，并记入 pending。
    单许可证（不含 | / &）未命中：保留原文，记入 pending。
    永远不返回 None（非空输入），映射不到时返回原文。
    """
    if pending is None:
        pending = set()
    if not text:
        return ""
    norm = _normalize(text)
    if not norm:
        return ""

    # 1. 整串精确命中（含复合 key）
    if norm in mapping:
        return mapping[norm]

    # 2. 不含组合操作符且未命中 -> 单许可证，保留原文并记入待处理
    if not any(ch in norm for ch in "|&"):
        pending.add(norm)
        return norm

    # 3. 按 | & 拆分成单个许可证逐个映射，保留操作符和分组括号；
    #    映射不到的单许可证保留原文，记入待处理
    out: list[str] = []
    for tok in _split_tokens(norm):
        if tok in ("|", "&", "(", ")"):
            out.append(tok)
        elif tok in mapping:
            out.append(mapping[tok])
        else:
            pending.add(tok)
            out.append(tok)
    return "".join(out)


def _map_record(record: dict, mapping: dict[str, str], pending: set | None = None) -> dict | None:
    """映射记录里的 license / license_text 字段，其余字段原样保留。

    返回映射后的新记录（浅拷贝，仅替换 license 字段值）；
    记录里没有任何 license / license_text 字段时返回 None。
    """
    field = None
    for f in ("license_text", "license"):
        val = record.get(f)
        if val is not None and str(val).strip():
            field = f
            break
    if field is None:
        return None
    result = _map_text(str(record[field]).strip(), mapping, pending)
    new = dict(record)
    new[field] = result
    return new


def full_mode(input_path: Path, dict_file: Path, dict_supplement: Path,
              output_mapped: Path, output_pending: Path) -> None:
    """全量映射（base + 补充映射），输出 mapped 结果 + 含未映射许可证的原文记录。"""
    mapping = load_mapping(dict_file)
    supplement = load_mapping(dict_supplement, skip_comments=True)
    if supplement:
        mapping.update(supplement)  # 补充映射可覆盖 base
        print(f"已合并补充映射 {len(supplement)} 条")
    print(f"映射表: {len(mapping)} 条")

    records = json.loads(input_path.read_text(encoding="utf-8"))
    print(f"输入记录: {len(records)} 条")

    mapped = []    # 完全映射的记录（license 已全部映射为标准标识）
    unmapped = []  # 含未映射许可证的记录（保留映射前原文，格式同 mapped）
    all_pending: set = set()
    for r in records:
        pending: set = set()  # 每条记录独立，用于判断是否含未映射的许可证
        rec = _map_record(r, mapping, pending)
        if rec is None:
            continue  # 无 license / license_text 字段的记录直接忽略
        if pending:
            unmapped.append(dict(r))  # 原文记录，license 为映射前原文
            all_pending.update(pending)
        else:
            mapped.append(rec)  # 只有完全映射的记录才进 mapped

    output_mapped.write_text(json.dumps(mapped, ensure_ascii=False, indent=2), encoding="utf-8")
    output_pending.write_text(json.dumps(unmapped, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完全映射: {len(mapped)} 条 -> {output_mapped.name}")
    print(f"含未映射许可证的记录: {len(unmapped)} 条 -> {output_pending.name}")
    print(f"未映射的许可证原文（去重 {len(all_pending)} 种）:")
    for x in sorted(all_pending):
        print(f"  - {x!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="将 license 字段按字典映射为标准标识符")
    ap.add_argument("input", type=Path, nargs="?", default=INPUT, help="输入 JSON 文件")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="输出 mapped 文件（默认 <input>_mapped.json）")
    ap.add_argument("--dict", type=Path, default=DICT_FILE, help="基础映射文件")
    ap.add_argument("--dict-supplement", type=Path, default=DICT_SUPPLEMENT, help="补充映射文件")
    args = ap.parse_args()

    inp = Path(args.input)
    output_mapped = args.output or inp.with_name(inp.stem + "_mapped.json")
    output_pending = inp.with_name(inp.stem + "_unmapped_licenses.json")
    full_mode(inp, args.dict, args.dict_supplement, output_mapped, output_pending)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
