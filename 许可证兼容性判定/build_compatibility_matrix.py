"""把许可证兼容性判定结果汇总成矩阵（行 = 原许可证，列 = 目标许可证）。

读取 兼容性判定结果/ 下所有 .json 判定结果，输出一个 CSV 矩阵：
    - 第一列是原许可证，表头是目标许可证
    - 单元格值为兼容性：0=兼容，1=不兼容，-1=待定（视情况）
    - 空单元格表示该对没有判定结果（自身对不判定）

用法：
    py build_compatibility_matrix.py
"""

import csv
import json
import time
from pathlib import Path

# ──── 全局配置（按需修改） ────
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "兼容性判定结果"/"测试结果_合并"      # 结果目录（递归读取所有 .json）
OUTPUT_FILE = BASE_DIR /"兼容性判定结果"/ "兼容性矩阵.csv"       # 输出矩阵 CSV

# 兼容性值的可读标签（仅用于打印统计，不影响矩阵数值）
COMPAT_LABEL = {0: "兼容", 1: "不兼容", -1: "待定"}


def collect_results(input_dir: Path) -> dict:
    """递归读取所有结果文件，返回 {(原许可证, 目标许可证): 兼容性}。"""
    results = {}
    for f in sorted(input_dir.rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        orig = data.get("原许可证")
        target = data.get("目标许可证")
        compat = data.get("兼容性")
        if orig is not None and target is not None:
            results[(orig, target)] = compat
    return results


def write_csv(results: dict, origs: list, targets: list, output_file: Path) -> None:
    """写出矩阵 CSV（行=原许可证，列=目标许可证）。"""
    header = ["原许可证\\目标许可证"] + targets
    with open(output_file, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for o in origs:
            row = [o]
            for t in targets:
                v = results.get((o, t))
                row.append("" if v is None else v)
            w.writerow(row)


def main() -> None:
    t_start = time.perf_counter()

    results = collect_results(INPUT_DIR)
    t_read = time.perf_counter()
    if not results:
        print(f"未在 {INPUT_DIR} 找到任何判定结果")
        return

    origs = sorted({k[0] for k in results})
    targets = sorted({k[1] for k in results})

    write_csv(results, origs, targets, OUTPUT_FILE)
    t_write = time.perf_counter()

    print(f"读取到 {len(results)} 个判定结果")
    print(f"原许可证（行）: {len(origs)} 个")
    print(f"目标许可证（列）: {len(targets)} 个")

    # 统计各兼容性数量
    from collections import Counter
    cnt = Counter(v for v in results.values())
    print("兼容性分布:", {COMPAT_LABEL.get(k, k): v for k, v in sorted(cnt.items(), key=lambda x: (x[0] is None, x[0]))})
    print(f"输出: {OUTPUT_FILE}")

    # 速度统计
    print("-" * 50)
    print(f"读取结果耗时: {t_read - t_start:.3f}s")
    print(f"写矩阵耗时:   {t_write - t_read:.3f}s")
    print(f"总耗时:       {t_write - t_start:.3f}s")


if __name__ == "__main__":
    main()
