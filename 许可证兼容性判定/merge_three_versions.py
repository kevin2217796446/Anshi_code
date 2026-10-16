# -*- coding: utf-8 -*-
"""三版本多数投票合并：多维度提取结果 / 1 / 2 -> 多维度合并结果1

用法：先重跑出 多维度提取结果2，再执行本脚本。
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent
V1 = BASE / "多维度提取结果"
V2 = BASE / "多维度提取结果1"
V3 = BASE / "多维度提取结果2"   # 最新（重跑结果）
PENDING = BASE / "多维度待定"
OUT = BASE / "多维度合并结果1"


def load_map(path):
    if not path.exists():
        return None
    return {it["维度"]: it for it in json.loads(path.read_text(encoding="utf-8"))}


def majority_vote(statuses):
    """statuses=[v1,v2,v3]，多数投票；平票取最新(v3，即最后一个)。"""
    valid = [s for s in statuses if s is not None]
    if not valid:
        return None
    cnt = Counter(valid)
    mx = max(cnt.values())
    winners = [s for s, c in cnt.items() if c == mx]
    if len(winners) == 1:
        return winners[0]
    return valid[-1]  # 平票，取最新的那个


def main():
    OUT.mkdir(exist_ok=True)
    names = sorted(f.stem for f in PENDING.glob("*.json"))
    done = 0
    skipped = 0
    tie_cnt = 0
    vote_by_dim = Counter()
    for name in names:
        m1 = load_map(V1 / f"{name}.json")
        m2 = load_map(V2 / f"{name}.json")
        m3 = load_map(V3 / f"{name}.json")
        if m3 is None:
            print(f"[跳过] {name}：多维度提取结果2 无结果")
            skipped += 1
            continue
        out_items = []
        for dim in m3.keys():
            s1 = m1[dim]["状态"] if (m1 and dim in m1) else None
            s2 = m2[dim]["状态"] if (m2 and dim in m2) else None
            s3 = m3[dim]["状态"] if (m3 and dim in m3) else None
            chosen = majority_vote([s1, s2, s3])
            non_null = [s for s in (s1, s2, s3) if s is not None]
            if len(set(non_null)) == 3:
                tie_cnt += 1
            vote_by_dim[chosen] += 1
            it3 = m3[dim]
            out_items.append({
                "维度": dim,
                "状态": chosen if chosen else it3.get("状态"),
                "原文": it3.get("原文", ""),
                "解释": it3.get("解释", ""),
            })
        (OUT / f"{name}.json").write_text(json.dumps(out_items, ensure_ascii=False, indent=2), encoding="utf-8")
        done += 1

    print(f"待定 {len(names)} 个：合并 {done} 个，跳过(缺v3) {skipped} 个")
    print(f"三版本全不同的维度(平票)数: {tie_cnt}")
    print(f"最终状态分布: {dict(vote_by_dim)}")
    print(f"输出: {OUT.name}/")


if __name__ == "__main__":
    main()
