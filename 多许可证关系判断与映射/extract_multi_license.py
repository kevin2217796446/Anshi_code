"""从 multi_license_mapped 结果中提取 normalized_license_names，转成 single_license_mapped_full 格式。

输入支持两种：
- JSON 数组（.json）：[ {...}, {...} ]
- JSONL（.jsonl）：每行一个 JSON 对象

规则：
- SINGLE（单许可证）：直接取该许可证。
- AND / OR（明确关系）：多许可证用 &（AND）或 |（OR）连接，符号两边无空格。
- NO_EXPLICIT_RELATION / UNKNOWN（无明确关系）：统一用 | 连接，并把 license_relation 原值存入输出。
- 明确关系或单许可证：输出里 license_relation 字段为空字符串。
- license_relation 为空（API 报错的记录）：跳过，另存到 --error-output。
"""

import argparse
import json
from collections import Counter
from pathlib import Path

BASE = Path(__file__).parent
DEFAULT_INPUT = BASE / "数据" / "0828maven爬取+多许可证判断" / "0828_test_output.jsonl"
DEFAULT_OUTPUT = BASE / "数据" / "0828maven爬取+多许可证判断" / "0828_test_output_extracted.jsonl"
DEFAULT_ERROR = BASE / "数据" / "0828maven爬取+多许可证判断" / "0828_test_output_extracted_error1.json"


def load_records(path: Path) -> list[dict]:
    """读取 JSON 数组或 JSONL 文件，返回记录列表。"""
    raw = path.read_text(encoding="utf-8").strip()
    # JSON 数组 [...]
    if raw.startswith("["):
        return json.loads(raw)
    # JSONL：每行一个 JSON 对象（跳过空行）
    records = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def combine(names: list[str], relation: str) -> str:
    """按 license_relation 组合许可证名称列表。"""
    if relation == "AND":
        return "&".join(names)
    if relation == "OR":
        return "|".join(names)
    if relation == "SINGLE":
        # 单许可证；异常出现多个时用 & 兜底，避免丢数据
        return names[0] if len(names) == 1 else "&".join(names)
    # NO_EXPLICIT_RELATION / UNKNOWN 等无明确关系 → 用 | 连接
    return "|".join(names)


def relation_field(relation: str) -> str:
    """明确关系或单许可证 → 空字符串；无明确关系 → 原值。"""
    if relation in ("SINGLE", "AND", "OR"):
        return ""
    return relation or ""


def main() -> None:
    ap = argparse.ArgumentParser(description="提取 multi_license 结果中的 normalized_license_names")
    ap.add_argument("input", type=Path, nargs="?", default=DEFAULT_INPUT,
                    help="输入文件（.json 数组 或 .jsonl）")
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="输出 JSON 文件")
    ap.add_argument("--error-output", type=Path, default=DEFAULT_ERROR, help="错误记录输出文件")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"文件不存在: {args.input}")
        return

    records = load_records(args.input)
    print(f"输入记录: {len(records)} 条")

    out, errors = [], []
    for r in records:
        relation = r.get("license_relation")
        if relation is None:  # API 报错等无有效结果
            errors.append(r)
            continue
        names = (r.get("normalized_license_names")
                 or r.get("effective_license_names")
                 or r.get("all_declared_license_names")
                 or [])
        out.append({
            "purl": r.get("purl", ""),
            "version": r.get("version", ""),
            "license": combine(names, relation),
            "license_relation": relation_field(relation),
        })

    args.output.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"输出记录: {len(out)} 条 -> {args.output.name}")

    if errors:
        args.error_output.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"跳过错误记录: {len(errors)} 条 -> {args.error_output.name}")

    rel_cnt = Counter(r["license_relation"] for r in out)
    print("输出 license_relation 分布:", dict(rel_cnt))

    # 抽样展示
    print("\n--- 抽样展示 ---")
    shown = set()
    for r in out:
        rel = r["license_relation"]
        if rel not in shown:
            shown.add(rel)
            print(f"  [{rel or '(空)'}] {json.dumps(r, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
