# -*- coding: utf-8 -*-
"""
map_json.py —— 第三步：用「审核通过」的去重表回填 need_check，并生成人工复核表。
================================================================================

【功能】
    main.py 对去重表做完「LLM 转换 + 审核」后，会得到 数据/非标审核数据/<stem>_verified.json，
    其中每条记录是：去重表里的 license_text + LLM 转出的 license_spdx_name + 审核结论 correct/confidence。

    本脚本用这张表做两件事：
        1. 审核通过（correct=true）的 license_text
           -> 对应的 need_check 原始行（未去重），带着映射结果追加到 数据/输出/<stem>_标准化结果.csv
        2. 审核未通过（correct=false）的 license_text
           -> 对应原始行留在 need_check，并额外生成一份「去重后」的未通过表
              数据/need_check/<stem>_审核未通过去重表.json，供人工复核（人工只看这一份去重表即可）

    人工改完去重表后，用 fill_need_check.py 把 need_check 中原本未去重的数据按改好的去重表映射回填。

【命令行】
    py map_json.py                         # 自动选 数据/非标审核数据 下最新 *_verified.json
    py map_json.py <verified.json 路径>     # 指定审核结果文件

【输入输出】
    - 审核结果:   数据/非标审核数据/<stem>_verified.json（JSONL 或 JSON 数组）
    - need_check: 数据/need_check/<stem>_need_check.json（第一步 map_csv.py 生成）
    - 人工复核表: 数据/need_check/<stem>_审核未通过去重表.json（本脚本生成）
    - 输出 CSV:   数据/输出/<stem>_标准化结果.csv（第一步 map_csv.py 生成表头，本脚本追加）
"""
import csv
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 让本脚本能 import 同目录的 data_tool（用于读取 JSONL）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_tool import load_jsonl  # noqa: E402

# ─── 路径（随脚本位置推导；部署到服务器后即为 /home/huanghaoran/llm_transform_new/... ）───
BASE_DIR = Path(__file__).resolve().parent.parent   # 非标转换机器部署
DATA_DIR = BASE_DIR / "数据"
VERIFY_DIR = DATA_DIR / "非标审核数据"
NEED_CHECK_DIR = DATA_DIR / "need_check"
OUTPUT_DIR = DATA_DIR / "输出"

# license 字段候选名（与 map_csv / llm_scan / verify_result 保持一致）
LICENSE_FIELDS = ("license_text", "license", "原许可证", "原始许可证")


def _license_text_of(record: dict) -> str:
    """从记录中提取许可证文本：优先 license_text，回退 license / 原许可证 / 原始许可证。"""
    for field in LICENSE_FIELDS:
        val = record.get(field)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _is_passed(record: dict) -> bool:
    """判断一条审核记录是否「审核通过」：correct=true（license_spdx_name 为 none 也视为通过，回填为 none）。"""
    return record.get("correct") is True


def _load_verified(path: Path) -> list[dict]:
    """读取审核结果文件，兼容 JSONL 与 JSON 数组。"""
    raw = path.read_text(encoding="utf-8").strip()
    if raw.startswith("["):
        return json.loads(raw)
    return load_jsonl(path)


def _read_csv_header(path: Path) -> tuple[list[str], str]:
    """读取输出 CSV 表头（兼容 utf-8-sig / gbk），返回 (header, 编码)。"""
    enc = "utf-8-sig"
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f))
    except UnicodeDecodeError:
        enc = "gbk"
        with open(path, encoding="gbk", newline="") as f:
            header = next(csv.reader(f))
    return header, enc


def _write_row(writer, item, header, license_col, license_value):
    """按输出表头写一行：license 列写映射结果，「原license」列写原始许可证文本，其余列取原始字段。"""
    row = []
    for col in header:
        if col == license_col:
            row.append(license_value)
        elif col == "原license":
            # 输出表头把原始「license」列改名成了「原license」，但 need_check 里的字段名仍是 license，
            # 所以这里用 _license_text_of 取原始许可证文本，避免写成空白。
            row.append(_license_text_of(item))
        else:
            row.append(item.get(col, ""))
    writer.writerow(row)


def _select_verified_file(specified):
    """确定审核结果文件：指定了就用它；否则选 非标审核数据 下最新 *_verified.json。"""
    if specified:
        return Path(specified)
    files = sorted(VERIFY_DIR.glob("*_verified.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"审核目录 {VERIFY_DIR} 下没有 *_verified.json，请先运行 main.py")
    return files[0]


def run_map_json(verified_file=None):
    """用审核通过的表回填 need_check，返回摘要 dict。"""
    verify_file = _select_verified_file(verified_file)

    # 从 <stem>_verified.json 反推 stem
    stem = verify_file.stem
    if stem.endswith("_verified"):
        stem = stem[: -len("_verified")]

    need_check_file = NEED_CHECK_DIR / f"{stem}_need_check.json"
    output_csv = OUTPUT_DIR / f"{stem}_标准化结果.csv"

    print(f'审核结果: {verify_file}', flush=True)
    print(f'need_check: {need_check_file}', flush=True)
    print(f'输出 CSV: {output_csv}', flush=True)

    if not need_check_file.exists():
        raise FileNotFoundError(f'未找到 need_check 文件: {need_check_file}（请先运行 module/map_csv.py）')
    if not output_csv.exists():
        raise FileNotFoundError(f'未找到输出 CSV: {output_csv}（请先运行 module/map_csv.py）')

    # 1. 读审核结果，构建 license_text -> 审核记录 映射
    verified = _load_verified(verify_file)
    verified_map = {}
    for rec in verified:
        # 允许 license_text 为空（""）：空许可证文本也作为一个 key，避免空 license 数据被丢弃
        verified_map[_license_text_of(rec)] = rec

    # 审核通过：correct=true（license_spdx_name 为 none 也回填为 none）
    passed_map = {lt: (rec.get("license_spdx_name") or "none") for lt, rec in verified_map.items() if _is_passed(rec)}
    # 审核未通过（correct=false）：去重后的数据，供人工复核。
    # 只保留「原始 license_text -> 修改后 license_spdx_name」映射所需字段，不带 record_id
    failed_dedup = [
        {
            "license_text": _license_text_of(rec),
            "license_spdx_name": rec.get("license_spdx_name") or "none",
            "correct": rec.get("correct"),
            "confidence": rec.get("confidence"),
            "count": rec.get("count"),
        }
        for rec in verified
        if not _is_passed(rec)
    ]

    print(f'审核记录 {len(verified)} 条；审核通过 {len(passed_map)} 条；审核未通过(去重) {len(failed_dedup)} 条', flush=True)

    # 2. 读 need_check（第一步未映射的原始行）与输出 CSV 表头
    need_check_data = json.loads(need_check_file.read_text(encoding="utf-8"))
    header, enc = _read_csv_header(output_csv)
    if "license" not in header:
        raise ValueError(f'输出 CSV 表头中未找到 license 列，表头={header}')
    license_col = "license"

    # 3. 分流：通过的追加到输出 CSV，未通过的留在 need_check（保持原本未去重的行）
    appended = 0
    remaining = []

    with open(output_csv, "a", encoding="utf-8" if enc == "utf-8-sig" else enc, newline="") as fo:
        writer = csv.writer(fo)
        for item in need_check_data:
            lt = _license_text_of(item)
            if lt in passed_map:
                _write_row(writer, item, header, license_col, passed_map[lt])
                appended += 1
            else:
                remaining.append(item)

    # 4. 覆盖写回 need_check（只剩审核未通过的原本未去重的数据）
    need_check_file.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5. 生成「去重后、审核未通过」的表，供人工复核（人工只看这一份去重表即可）
    failed_dedup_file = NEED_CHECK_DIR / f"{stem}_审核未通过去重表.json"
    failed_dedup_file.write_text(json.dumps(failed_dedup, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6. 汇总与数量校验
    total_unmapped = len(need_check_data)
    mapped_before = _count_csv_rows(output_csv) - appended  # 追加前的行数
    final_output_rows = mapped_before + appended
    total_input = mapped_before + total_unmapped

    print('\n' + '=' * 60, flush=True)
    print(f'[完成] need_check 原始 {total_unmapped} 条', flush=True)
    print(f'  审核通过 -> 追加到输出 CSV: {appended} 条', flush=True)
    print(f'  审核未通过 -> 留在 need_check: {len(remaining)} 条', flush=True)
    print(f'  人工复核表(去重后): {failed_dedup_file}（{len(failed_dedup)} 条）', flush=True)
    ok = "✅" if appended + len(remaining) == total_unmapped else "❌"
    print(f'  校验 {ok}: 通过({appended}) + 剩余({len(remaining)}) = {appended + len(remaining)} == need_check 原始({total_unmapped})', flush=True)
    print(f'  输出 CSV 总行数: {final_output_rows}（映射 {mapped_before} + 回填 {appended}）', flush=True)
    print(f'  数量守恒: 输出 CSV({final_output_rows}) + need_check({len(remaining)}) = {final_output_rows + len(remaining)} == 输入({total_input})', flush=True)

    return {
        "verify_file": str(verify_file),
        "stem": stem,
        "output_csv": str(output_csv),
        "need_check_json": str(need_check_file),
        "failed_dedup_json": str(failed_dedup_file),
        "appended": appended,
        "remaining": len(remaining),
        "failed_dedup_n": len(failed_dedup),
        "final_output_rows": final_output_rows,
        "total_input": total_input,
    }


def _count_csv_rows(path: Path) -> int:
    """统计 CSV 数据行数（不含表头）。"""
    header, enc = _read_csv_header(path)
    n = 0
    with open(path, encoding="utf-8" if enc == "utf-8-sig" else enc, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # 跳过表头
        for _ in reader:
            n += 1
    return n


def main():
    args_verified = sys.argv[1] if len(sys.argv) > 1 else None
    run_map_json(args_verified)


if __name__ == '__main__':
    main()
