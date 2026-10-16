#!/usr/bin/env python3
"""多许可证关系判断与映射 —— 全流程主程序。

流程：
1. classify_license_relations  从 数据/Data_input 读数据，单/多许可证判定，结果存 数据/Data_classify_result
2. extract_multi_license       从 数据/Data_classify_result 提取 normalized_license_names，存 数据/Data_extract_result
3. map_license_dict            将上一步结果按字典映射为标准标识符（完全映射 / 未映射 分开）
4. dedupe_license              对完全映射结果去重，再与未映射的许可证原文合并，输出完整结果（JSON + CSV）到 数据/Data_output

用法：
    py main.py                              # 自动选 Data_input 下最新的 JSON，跑全流程
    py main.py --input <文件.json>           # 指定输入
    py main.py --model deepseek-v4-pro --workers 8   # 命令行覆盖参数
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK，遇到特殊字符会崩，统一转 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════
# ─── 全局配置（按需修改，命令行参数可覆盖）───
# ═══════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "数据"

# ── 目录 ──
INPUT_DIR = DATA_DIR / "Data_input"               # 输入数据目录
CLASSIFY_DIR = DATA_DIR / "Data_classify_result"  # 第 1 步：分类结果
EXTRACT_DIR = DATA_DIR / "Data_extract_result"    # 第 2 步：提取结果（第 3 步映射结果也放这里）
OUTPUT_DIR = DATA_DIR / "Data_output"                     # 第 4 步：去重后的最终结果

# ── 输入文件（None = 自动选 INPUT_DIR 下最新创建的 .json）──
INPUT_FILE = None

# ── 映射字典（在上级项目的 数据/原始数据 里）──
DICT_FILE = BASE_DIR.parent / "数据" / "原始数据" / "dict_correct_new.txt"
DICT_SUPPLEMENT = BASE_DIR.parent / "数据" / "原始数据" / "dict_correct_new_supplement.txt"

# ── classify_license_relations 参数 ──
MODEL = "deepseek-v4-flash"
API_URL = "https://api.deepseek.com/chat/completions"
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")   # 空 = 用 classify 脚本自身的默认 key
WORKERS = 10
MAX_SINGLE_CHARS = 100          # 单许可证 license_text 最大长度，超过视为多许可证
MAX_LICENSE_CHARS = 8000        # 送入模型的 license_text 最大字符数
MAX_POM_CHARS = 12000           # 送入模型的 POM 上下文最大字符数
MAX_TOKENS = 4096
RETRIES = 3
TIMEOUT = 120
MAX_RECORDS = None              # 仅处理前 N 条多许可证，便于抽样；None = 全部

# ── 下游刷新间隔（秒）：classify 运行期间，每隔该时间把已产出的结果跑一遍 extract->map->dedupe ──
INTERVAL = 20

# ── 各步骤脚本路径 ──
CLASSIFY_SCRIPT = BASE_DIR / "classify_license_relations.py"
EXTRACT_SCRIPT = BASE_DIR / "extract_multi_license.py"
MAP_SCRIPT = BASE_DIR / "map_license_dict.py"
DEDUPE_SCRIPT = BASE_DIR / "dedupe_license.py"
# ═══════════════════════════════════════════════════════════════════


def _pick_input() -> Path:
    """确定输入文件：配置了 INPUT_FILE 用它；否则选 Data_input 下最新创建的 .json。"""
    if INPUT_FILE:
        return Path(INPUT_FILE)
    files = sorted(
        INPUT_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"默认输入目录 {INPUT_DIR} 下没有 JSON 文件")
    return files[0]


def _cmd(script: Path, args: list[str]) -> list[str]:
    return [sys.executable, str(script), *args]


def _run(script: Path, args: list[str]) -> int:
    """以子进程方式运行一个步骤脚本（阻塞），返回退出码。"""
    cmd = _cmd(script, args)
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd).returncode


def _start(script: Path, args: list[str]) -> subprocess.Popen:
    """后台启动一个步骤脚本（不阻塞），返回 Popen。"""
    cmd = _cmd(script, args)
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    return subprocess.Popen(cmd)


def _format_duration(seconds: float) -> str:
    """把秒数格式化为可读的「X小时X分X秒 / X分X秒 / X秒」。"""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _combine(dedupe_file: Path, unmapped_file: Path, complete_file: Path) -> bool:
    """合并 dedupe（完全映射）与 unmapped_license（未映射原文）为完整结果。

    返回 False 表示出错；dedupe 尚未生成时返回 True（跳过，等待下次下游刷新）。
    """
    try:
        dedupe = json.loads(dedupe_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True  # dedupe 尚未生成，跳过
    except Exception as e:
        print(f"[合并] 读取 {dedupe_file.name} 失败: {e}", flush=True)
        return False

    unmapped = []
    if unmapped_file.exists():
        try:
            unmapped = json.loads(unmapped_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[合并] 读取 {unmapped_file.name} 失败: {e}", flush=True)
            return False

    complete = dedupe + unmapped
    tmp = complete_file.with_suffix(complete_file.suffix + ".tmp")
    tmp.write_text(json.dumps(complete, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, complete_file)
    print(f"[合并] 完整结果: {len(dedupe)} 条映射 + {len(unmapped)} 条未映射 = {len(complete)} 条 -> {complete_file.name}", flush=True)
    return True


def _to_csv(json_file: Path, csv_file: Path) -> bool:
    """把完整结果 JSON 转成 CSV（列名取所有记录 key 的并集，按首次出现顺序）。

    返回 False 表示出错；JSON 尚未生成时返回 True（跳过，等待下次下游刷新）。
    """
    try:
        records = json.loads(json_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True  # JSON 尚未生成，跳过
    except Exception as e:
        print(f"[转CSV] 读取 {json_file.name} 失败: {e}", flush=True)
        return False

    fieldnames: list[str] = []
    for rec in records:
        if isinstance(rec, dict):
            for k in rec.keys():
                if k not in fieldnames:
                    fieldnames.append(k)

    tmp = csv_file.with_suffix(csv_file.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            if isinstance(rec, dict):
                writer.writerow({k: rec.get(k, "") for k in fieldnames})
    os.replace(tmp, csv_file)
    print(f"[转CSV] {len(records)} 条 -> {csv_file.name}", flush=True)
    return True


def _classify_args(input_path: Path, output_path: Path, a: argparse.Namespace) -> list[str]:
    """组装 classify_license_relations.py 的命令行参数。"""
    argv = [
        str(input_path), "-o", str(output_path),
        "--model", a.model,
        "--api-url", a.api_url,
        "--workers", str(a.workers),
        "--max-single-chars", str(a.max_single_chars),
        "--max-license-chars", str(a.max_license_chars),
        "--max-pom-chars", str(a.max_pom_chars),
        "--max-tokens", str(a.max_tokens),
        "--retries", str(a.retries),
        "--timeout", str(a.timeout),
    ]
    if a.api_key:
        argv += ["--api-key", a.api_key]
    if a.max_records:
        argv += ["--max-records", str(a.max_records)]
    return argv


def run_pipeline(a: argparse.Namespace, input_file: Path) -> int:
    stem = input_file.stem
    t_start = time.time()  # 总运行时长起点

    for d in (CLASSIFY_DIR, EXTRACT_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    classify_out = CLASSIFY_DIR / f"{stem}_classify.jsonl"
    extract_out = EXTRACT_DIR / f"{stem}_extract.json"
    map_out = EXTRACT_DIR / f"{stem}_extract_mapped.json"
    unmapped_out = EXTRACT_DIR / f"{stem}_extract_unmapped_licenses.json"
    dedupe_out = OUTPUT_DIR / f"{stem}_dedupe.json"
    complete_out = OUTPUT_DIR / f"{stem}_complete.json"
    complete_csv = OUTPUT_DIR / f"{stem}_complete.csv"

    print("=" * 60)
    print(f"输入文件: {input_file}")
    print(f"模型: {a.model}（并发 {a.workers}）")
    print(f"下游刷新间隔: {a.interval} 秒")
    print("=" * 60)

    # 1. classify 后台启动：单/多许可证判定，逐条写入 Data_classify_result
    print(f"\n[1] classify_license_relations 后台启动 -> {classify_out}")
    t_classify_start = time.time()
    proc = _start(CLASSIFY_SCRIPT, _classify_args(input_file, classify_out, a))
    refresh_count = 0

    # 2-4. 每 interval 秒，把 classify 已产出的结果跑一遍 extract -> map -> dedupe
    def downstream() -> bool:
        if not classify_out.exists() or classify_out.stat().st_size == 0:
            print(f"[下游刷新] 跳过：{classify_out.name} 尚无可处理数据", flush=True)
            return True
        print(f"\n[下游刷新] extract -> map -> dedupe", flush=True)
        if _run(EXTRACT_SCRIPT, [
                str(classify_out), "-o", str(extract_out),
                "--error-output", str(EXTRACT_DIR / f"{stem}_extract_error.json"),
        ]) != 0:
            return False
        if _run(MAP_SCRIPT, [
                str(extract_out), "-o", str(map_out),
                "--dict", str(a.dict_file), "--dict-supplement", str(a.dict_supplement),
        ]) != 0:
            return False
        if _run(DEDUPE_SCRIPT, [str(map_out), "-o", str(dedupe_out)]) != 0:
            return False
        # 合并 dedupe（完全映射）与 unmapped_license（未映射原文）为完整结果
        if not _combine(dedupe_out, unmapped_out, complete_out):
            return False
        # 完整结果转 CSV
        if not _to_csv(complete_out, complete_csv):
            return False
        return True

    # 循环：每隔 interval 秒刷新一次下游，直到 classify 结束
    while proc.poll() is None:
        time.sleep(a.interval)
        if proc.poll() is not None:
            break
        downstream()
        refresh_count += 1

    # classify 结束后，最后刷新一次，收尾最后一批结果
    downstream()
    rc = proc.wait()
    t_classify_end = time.time()

    t_end = time.time()
    print("\n" + "=" * 60)
    print(f"classify 退出码 {rc}，最终结果：{complete_out} / {complete_csv}（映射去重 {dedupe_out} + 未映射 {unmapped_out}）")
    print(f"classify 运行时长: {_format_duration(t_classify_end - t_classify_start)}")
    print(f"下游刷新次数: {refresh_count} 次")
    print(f"总运行时长: {_format_duration(t_end - t_start)}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="多许可证关系判断与映射全流程")
    ap.add_argument("--input", type=Path, default=INPUT_FILE,
                    help="输入 JSON 文件（默认 Data_input 下最新）")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--api-url", default=API_URL)
    ap.add_argument("--api-key", default=API_KEY)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--max-single-chars", type=int, default=MAX_SINGLE_CHARS)
    ap.add_argument("--max-license-chars", type=int, default=MAX_LICENSE_CHARS)
    ap.add_argument("--max-pom-chars", type=int, default=MAX_POM_CHARS)
    ap.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    ap.add_argument("--retries", type=int, default=RETRIES)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    ap.add_argument("--max-records", type=int, default=MAX_RECORDS,
                    help="仅处理前 N 条多许可证，便于抽样（默认全部）")
    ap.add_argument("--interval", type=int, default=INTERVAL,
                    help="下游刷新间隔（秒），默认 20")
    ap.add_argument("--dict-file", type=Path, default=DICT_FILE, help="基础映射字典")
    ap.add_argument("--dict-supplement", type=Path, default=DICT_SUPPLEMENT, help="补充映射字典")
    args = ap.parse_args()

    input_file = args.input or _pick_input()
    if not input_file.exists():
        print(f"输入文件不存在: {input_file}", file=sys.stderr)
        return 1
    return run_pipeline(args, input_file)


if __name__ == "__main__":
    raise SystemExit(main())
