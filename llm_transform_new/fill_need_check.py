"""把「人工审核通过」的去重表结果，映射回填到 数据/输出 对应的 CSV。

逻辑：
- 读 数据/need_check 下每个 *_审核未通过去重表.json（人工已修改 correct 字段）。
- 对其中 correct 不等于 false（含没有 correct 字段）的记录，用 license_text -> license_spdx_name 建立映射。
- 找到同名前缀的 *_need_check.json（原本未去重的数据）与 输出 下 *_标准化结果.csv。
- 用上面的映射，把 *_need_check.json 中 license_text 命中的记录回填到 CSV 末尾。
- 未命中的记录仍留在 need_check（继续人工处理）。

用法：
    py fill_need_check.py                                   # 处理 need_check 目录下所有审核未通过去重表
    py fill_need_check.py <目录或 *_审核未通过去重表.json>     # 指定目录或单个文件
"""

import csv
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
NEED_CHECK_DIR = BASE_DIR / "数据" / "need_check"    # 需复核数据目录
OUTPUT_DIR = BASE_DIR / "数据" / "输出"              # 标准化结果 CSV 目录

FAILED_DEDUP_SUFFIX = "_审核未通过去重表"             # 人工复核文件后缀

# license 字段候选名（与 map_csv / map_json 保持一致）
LICENSE_FIELDS = ("license_text", "license", "原许可证", "原始许可证")


def _license_text_of(record: dict) -> str:
    """从记录中提取许可证文本：优先 license_text，回退 license / 原许可证 / 原始许可证。"""
    for field in LICENSE_FIELDS:
        val = record.get(field)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _is_false(v) -> bool:
    """判断 correct 字段是否为 false（兼容布尔 False 和字符串 'false'）。"""
    return v is False or (isinstance(v, str) and v.strip().lower() == "false")


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


def fill_one(failed_dedup_file: Path, need_check_file: Path, output_csv: Path) -> tuple[int, int]:
    """用人工审核通过的去重表，把原本未去重的 need_check 数据回填到输出 CSV。

    返回 (回填条数, 剩余 need_check 条数)。
    """
    # 1. 读人工审核通过的去重表，建立「原始 license_text -> 修改后 license_spdx_name」映射。
    #    通过条件：correct 不等于 false（含没有 correct 字段）。
    #    注意：按 license_text 匹配，不按 record_id（去重表的 record_id 与 need_check 未去重行的 record_id 无关）。
    failed_dedup = json.loads(failed_dedup_file.read_text(encoding="utf-8"))
    approved_map = {}
    for rec in failed_dedup:
        if not _is_false(rec.get("correct")):
            lt = _license_text_of(rec)
            # 允许 license_text 为空（""）的情况：空许可证文本也作为一个映射 key，
            # 这样 need_check 里 license 为空的数据也能被回填成 license_spdx_name（如 no_license）
            approved_map[lt] = rec.get("license_spdx_name") or "none"

    # 2. 读原本未去重的 need_check 数据与输出 CSV 表头
    need_check_data = json.loads(need_check_file.read_text(encoding="utf-8"))
    header, enc = _read_csv_header(output_csv)
    if "license" not in header:
        raise ValueError(f'输出 CSV 表头中未找到 license 列，表头={header}')
    license_col = "license"

    # 3. 按映射分流：命中 -> 追加到输出 CSV；未命中 -> 留在 need_check
    appended = 0
    remaining = []
    with open(output_csv, "a", encoding="utf-8" if enc == "utf-8-sig" else enc, newline="") as fo:
        writer = csv.writer(fo)
        for item in need_check_data:
            lt = _license_text_of(item)
            if lt in approved_map:
                _write_row(writer, item, header, license_col, approved_map[lt])
                appended += 1
            else:
                remaining.append(item)

    # 4. 覆盖写回 need_check（只剩仍未通过的原本未去重的数据）
    need_check_file.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
    return appended, len(remaining)


def main() -> None:
    # 支持目录（处理所有审核未通过去重表）或单个文件（只处理这一个）
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else NEED_CHECK_DIR
    if target.is_file():
        failed_files = [target]
    elif target.is_dir():
        failed_files = sorted(target.glob(f"*{FAILED_DEDUP_SUFFIX}.json"))
    else:
        print(f"{target} 不存在（既不是文件也不是目录）")
        return

    if not failed_files:
        print(f"{target} 下没有 *{FAILED_DEDUP_SUFFIX}.json 文件")
        return

    for ff in failed_files:
        stem = ff.stem[: -len(FAILED_DEDUP_SUFFIX)]
        need_check_file = NEED_CHECK_DIR / f"{stem}_need_check.json"
        output_csv = OUTPUT_DIR / f"{stem}_标准化结果.csv"
        if not need_check_file.exists():
            print(f"未找到对应的 need_check 文件: {need_check_file}")
            continue
        if not output_csv.exists():
            print(f"未找到对应的输出 CSV: {output_csv}")
            continue
        appended, remaining = fill_one(ff, need_check_file, output_csv)
        print(f"{ff.name} -> {output_csv.name}: 回填 {appended} 条，剩余 need_check {remaining} 条")


if __name__ == "__main__":
    main()
