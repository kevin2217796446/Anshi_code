# -*- coding: utf-8 -*-
"""比较多维度提取结果 与 多维度提取结果1 的 14 维度差异，生成合并结果与待定清单。"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent
ORIG_DIR = BASE / "多维度提取结果"
NEW_DIR = BASE / "多维度提取结果1"
PENDING_DIR = BASE / "多维度待定"
MERGED_DIR = BASE / "多维度合并结果"

CERTAIN = {"明确允许", "明确禁止", "未涉及"}


def load_items(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    PENDING_DIR.mkdir(exist_ok=True)
    MERGED_DIR.mkdir(exist_ok=True)

    orig_files = {f.stem: f for f in ORIG_DIR.glob("*.json")}
    new_files = {f.stem: f for f in NEW_DIR.glob("*.json")}
    common = sorted(set(orig_files) & set(new_files))
    only_orig = sorted(set(orig_files) - set(new_files))
    only_new = sorted(set(new_files) - set(orig_files))

    n_total = len(common)
    n_opposite = 0   # 待定（有相反维度）
    n_merged = 0     # 合并（有模糊->确定 或 其他差异）
    n_same = 0       # 完全一致
    opposite_by_dim = Counter()
    fuzzy_by_dim = Counter()
    other_by_dim = Counter()

    for name in common:
        orig_items = load_items(orig_files[name])
        new_items = load_items(new_files[name])
        new_map = {it["维度"]: it for it in new_items}

        opposite, fuzzy, other = [], [], []
        resolved = []
        for o in orig_items:  # 按原数据顺序 1..14
            dim = o["维度"]
            n = new_map.get(dim)
            if n is None:
                resolved.append(o)
                continue
            s1, s2 = o["状态"], n["状态"]
            if s1 == s2:
                resolved.append(n)
            elif {s1, s2} == {"明确允许", "明确禁止"}:
                opposite.append((dim, s1, s2))
                resolved.append(n)
            elif s1 == "模糊" and s2 in CERTAIN:
                fuzzy.append((dim, "原=模糊→新确定", s2))
                resolved.append(n)
            elif s2 == "模糊" and s1 in CERTAIN:
                fuzzy.append((dim, "新=模糊→原确定", s1))
                resolved.append(o)
            else:
                # 确定但不同（如 明确允许 vs 未涉及），取有明确立场的一方
                other.append((dim, s1, s2))
                resolved.append(o if s1 in ("明确允许", "明确禁止") else n)

        if opposite:
            n_opposite += 1
            for dim, s1, s2 in opposite:
                opposite_by_dim[dim] += 1
            rec = {
                "许可证": name,
                "相反维度": [{"维度": d, "原": a, "新": b} for d, a, b in opposite],
                "模糊已解析": [{"维度": d, "说明": note, "采用": val} for d, note, val in fuzzy],
                "其他差异": [{"维度": d, "原": a, "新": b} for d, a, b in other],
            }
            (PENDING_DIR / f"{name}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            if fuzzy or other:
                n_merged += 1
                for dim, note, val in fuzzy:
                    fuzzy_by_dim[dim] += 1
                for dim, s1, s2 in other:
                    other_by_dim[dim] += 1
            else:
                n_same += 1
            (MERGED_DIR / f"{name}.json").write_text(
                json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = {
        "总许可证数": n_total,
        "待定_有相反维度": n_opposite,
        "合并_有模糊或差异": n_merged,
        "完全一致": n_same,
        "仅在原数据": only_orig,
        "仅在新数据": only_new,
        "各维度相反次数": dict(opposite_by_dim),
        "各维度模糊解析次数": dict(fuzzy_by_dim),
        "各维度其他差异次数": dict(other_by_dim),
    }
    (BASE / "维度对比统计.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 60)
    print(f"总许可证数: {n_total}")
    print(f"待定(有相反维度): {n_opposite}")
    print(f"合并(有模糊/差异): {n_merged}")
    print(f"完全一致: {n_same}")
    if only_orig:
        print(f"仅在原数据(新数据缺 {len(only_orig)}): {only_orig}")
    if only_new:
        print(f"仅在新数据(原数据缺 {len(only_new)}): {only_new}")
    print()
    print("各维度相反次数:", dict(opposite_by_dim))
    print("各维度模糊解析次数:", dict(fuzzy_by_dim))
    print("各维度其他差异次数:", dict(other_by_dim))
    print()
    print("待定写入:", PENDING_DIR.name)
    print("合并结果写入:", MERGED_DIR.name)
    print("统计写入: 维度对比统计.json")


if __name__ == "__main__":
    main()
