# -*- coding: utf-8 -*-
"""
map_csv.py —— 第一步：按知识库字典把输入 CSV 的 license 字段做映射，分流到「输出」与「need_check」。
================================================================================

【功能】
    1. 读 数据/输入 下的 CSV（默认自动选最新），用下面两个字典对 license 字段做「整体映射」：
         - dict_correct_new.txt             base（tab 分隔 key<TAB>value）
         - dict_correct_new_supplement.txt  supplement（tab 或双空格分隔，支持 # 注释）
       命中映射的       -> 直接写 数据/输出/<stem>_标准化结果.csv
       未命中映射的     -> 先整体写入 数据/need_check/<stem>_need_check.json，
                          再去重生成 数据/去重表/<stem>.json（供后续大模型转换 + 审核）
    2. 保证：输出 CSV 行数 + need_check 行数 == 输入 CSV 行数（不丢数据）。

【license 字段】自动识别 license_text / license / 原许可证 / 原始许可证。

【输出目录】
    - 数据/输出/<stem>_标准化结果.csv    命中映射的数据（含新增 license 列）
    - 数据/need_check/<stem>_need_check.json   未命中映射的数据（保留原始字段 + record_id）
    - 数据/去重表/<stem>.json            未命中数据去重后的 license_text 表（大模型输入）

【命令行】
    py map_csv.py                 # 自动选 数据/输入 下最新 CSV
    py map_csv.py -i <csv路径>     # 指定输入 CSV

【后续步骤】
    main.py          -> 大模型转换 + 审核（输入 = 数据/去重表/<stem>.json）
    map_json.py      -> 用审核通过的表回填 need_check，剩余 need_check 交给人工
    fill_need_check.py -> 人工审核后回填到输出 CSV
"""
import argparse
import csv
import io
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 大型字段（如 pom_text）可能超过 csv 默认 128KB 限制
csv.field_size_limit(10 ** 9)

# 让本脚本能 import 同目录的 data_tool（用于生成 record_id）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_tool import record_id  # noqa: E402

# ─── 路径（随脚本位置推导；部署到服务器后即为 /home/huanghaoran/llm_transform_new/... ）───
BASE_DIR = Path(__file__).resolve().parent.parent   # 非标转换机器部署
DATA_DIR = BASE_DIR / "数据"
INPUT_DIR = DATA_DIR / "输入"
KNOWLEDGE_DIR = DATA_DIR / "知识库数据"
OUTPUT_DIR = DATA_DIR / "输出"
NEED_CHECK_DIR = DATA_DIR / "need_check"
DEDUP_DIR = DATA_DIR / "去重表"          # 新增：去重表文件夹

DICT_BASE = KNOWLEDGE_DIR / "dict_correct_new.txt"
DICT_SUPPLEMENT = KNOWLEDGE_DIR / "dict_correct_new_supplement.txt"

# license 字段候选名（与 llm_scan / verify_result / main 保持一致）
LICENSE_FIELDS = ("license_text", "license", "原许可证", "原始许可证")


def load_mapping(path, skip_comments=False):
    """从 TXT 字典加载 key→value 映射。tab 分隔优先，否则按 2 个以上空格分隔。"""
    if not path or not os.path.exists(path):
        return {}
    mapping = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if skip_comments and line.startswith('#'):
                continue
            if '\t' in line:
                k, v = line.split('\t', 1)
                mapping[k.strip()] = v.strip()
            else:
                parts = re.split(r'\s{2,}', line)
                if len(parts) >= 2:
                    mapping[parts[0].strip()] = parts[-1].strip()
    return mapping


_OP_RE = re.compile(r'\s*([|&])\s*')


def _normalize_ops(text):
    """把 SPDX 逻辑操作符 OR / AND 换成 | / &（两边不留空格），大小写不敏感。
    "or later" / "and later"（如 "GPL v3.0 or later"）不是逻辑组合，保留不切开。"""
    text = re.sub(r'\s+or\s+(?!later\b)', '|', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+and\s+(?!later\b)', '&', text, flags=re.IGNORECASE)
    return text


def _normalize(text):
    """折叠空白、去掉 | / & 两边的空格，得到字典查找键。"""
    text = re.sub(r'\s+', ' ', text).strip()
    return _OP_RE.sub(r'\1', text)


def _license_field_of(header):
    """找出 CSV 表头里的 license 列。"""
    return next((f for f in LICENSE_FIELDS if f in header), None)


def _to_int(value, default=1):
    """把 count 字段转成 int（可能是 '190' 或 190），失败/缺失返回 default。"""
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def _output_header(header, license_field):
    """推导输出 CSV 表头：license 列改名为「原license」并在其后新增「license」列。"""
    orig_license_name = "原license" if license_field == "license" else license_field
    out = []
    for col in header:
        if col == license_field:
            out.append(orig_license_name)
            out.append("license")
        else:
            out.append(col)
    return out


def _write_row(writer, item, header, license_field, license_value):
    """按输出表头写一行：license 列位置写映射结果，其余列原样。"""
    row = []
    for col in header:
        if col == license_field:
            row.append(item.get(col, ""))
            row.append(license_value)
        else:
            row.append(item.get(col, ""))
    writer.writerow(row)


def _select_input_file(input_dir, specified):
    """确定输入 CSV：指定了就用它；否则选输入目录下创建时间最新的 CSV。"""
    if specified:
        return Path(specified)
    files = sorted(input_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"输入目录 {input_dir} 下没有 CSV 文件")
    return files[0]


def _read_csv_text(path):
    """读取 CSV 文本，自动探测编码：utf-8-sig 优先，失败回退 gbk。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="replace")  # 兜底，避免彻底失败


def run_map(input_file=None):
    """执行字典映射 + 去重，返回摘要 dict（含 dedup_json 等输出路径）。"""
    # 1. 加载映射字典（base + supplement，后者覆盖同名 key）
    base_mapping = load_mapping(DICT_BASE)
    supplement = load_mapping(DICT_SUPPLEMENT, skip_comments=True)
    mapping = dict(base_mapping)
    if supplement:
        mapping.update(supplement)
    print(f'映射表加载: base={len(base_mapping)} 条, supplement={len(supplement)} 条, 合并后={len(mapping)} 条', flush=True)

    # 2. 确定输入 CSV 与各输出路径
    input_file = _select_input_file(INPUT_DIR, input_file)
    stem = input_file.stem
    output_csv = OUTPUT_DIR / f"{stem}_标准化结果.csv"
    need_check_json = NEED_CHECK_DIR / f"{stem}_need_check.json"
    dedup_json = DEDUP_DIR / f"{stem}.json"

    for d in (OUTPUT_DIR, NEED_CHECK_DIR, DEDUP_DIR):
        d.mkdir(parents=True, exist_ok=True)

    print(f'输入: {input_file}', flush=True)
    print(f'输出 CSV: {output_csv}', flush=True)
    print(f'need_check: {need_check_json}', flush=True)
    print(f'去重表: {dedup_json}', flush=True)

    # 3. 逐行映射
    total = 0
    mapped_n = 0
    unmapped_n = 0
    need_check_rows = []   # 未映射的原始行（含 record_id）
    dedup = {}             # license_text(原始) -> {"license_text": ..., "count": int}

    reader = csv.reader(io.StringIO(_read_csv_text(input_file)))
    header = next(reader)
    license_field = _license_field_of(header)
    if license_field is None:
        raise ValueError(f'输入 CSV 表头中未找到 license 列（候选：{LICENSE_FIELDS}），表头={header}')
    lt_idx = header.index(license_field)

    with open(output_csv, 'w', encoding='utf-8-sig', newline='') as fo:
        writer = csv.writer(fo)
        writer.writerow(_output_header(header, license_field))

        for row in reader:
            total += 1
            rec = dict(zip(header, row))
            lt = (rec.get(license_field) or "").strip()
            # 归一化后查字典（整体映射，不做拆分）
            norm = _normalize(_normalize_ops(lt))

            if norm in mapping:
                # 命中 -> 直接写输出 CSV
                _write_row(writer, rec, header, license_field, mapping[norm])
                mapped_n += 1
            else:
                # 未命中 -> 进 need_check；非空 license 才进入去重表
                rec_with_id = dict(rec)
                rec_with_id["record_id"] = record_id(rec)
                need_check_rows.append(rec_with_id)
                unmapped_n += 1

                if lt:  # 空许可证文本不进去重表（交给人工）
                    key = lt
                    if key in dedup:
                        dedup[key]["count"] += _to_int(rec.get("count"))
                    else:
                        dedup[key] = {"license_text": lt, "count": _to_int(rec.get("count"))}

            if total % 500000 == 0:
                print(f'  已处理 {total} 条 (mapped={mapped_n}, unmapped={unmapped_n})', flush=True)

    # 4. 写 need_check（JSON 数组）与去重表（JSON 数组，供大模型转换）
    need_check_json.write_text(json.dumps(need_check_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    dedup_json.write_text(json.dumps(list(dedup.values()), ensure_ascii=False, indent=2), encoding="utf-8")

    # 5. 汇总与数量校验
    print('\n' + '=' * 60, flush=True)
    print(f'[完成] 输入 {total} 条', flush=True)
    print(f'  命中映射 -> 输出 CSV: {mapped_n} 条', flush=True)
    print(f'  未命中   -> need_check: {unmapped_n} 条', flush=True)
    print(f'  去重表: {len(dedup)} 条唯一 license_text', flush=True)
    balance = mapped_n + unmapped_n
    ok = "✅" if balance == total else "❌"
    print(f'  数量校验 {ok}: 输出({mapped_n}) + need_check({unmapped_n}) = {balance} == 输入({total})', flush=True)

    return {
        "input_file": str(input_file),
        "stem": stem,
        "output_csv": str(output_csv),
        "need_check_json": str(need_check_json),
        "dedup_json": str(dedup_json),
        "total": total,
        "mapped_n": mapped_n,
        "unmapped_n": unmapped_n,
    }


def main():
    parser = argparse.ArgumentParser(description='按知识库字典对输入 CSV 的 license 字段做映射，分流 输出/need_check')
    parser.add_argument('--input', '-i', default=None, help='输入 CSV 路径（默认：数据/输入 下最新 CSV）')
    args = parser.parse_args()
    run_map(args.input)


if __name__ == '__main__':
    main()
