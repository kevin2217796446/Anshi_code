"""非标许可证转换 + 审核全流程入口（一条命令跑完「人工审核前」的所有步骤）。

【程序用途】
把「非标许可证文本」 通1过 LLM 标准化成 SPDX / 标准许可证标识，再审核标准化结果是否可信。

【整体流程】（本脚本自动串起，只需运行一次）
    1. map_csv      （第一步）：按知识库字典对输入 CSV 的 license 字段做整体映射
       - 命中映射 -> 直接写 数据/输出/<stem>_标准化结果.csv
       - 未命中   -> 写 数据/need_check/<stem>_need_check.json，并去重生成 数据/去重表/<stem>.json
    2. llm_scan     （第二步）：对「去重表」做 LLM 标准化 -> 数据/非标转换数据/<stem>_output.json
    3. verify_result（第三步）：审核标准化结果 -> 数据/非标审核数据/<stem>_verified.json
    4. map_json     （第四步）：用审核通过的表回填 need_check 到输出 CSV，
       剩余（审核未通过）留在 need_check 交给人工

人工审核后，运行 fill_need_check.py 把 correct != false 的数据回填输出 CSV。

【输入】
    CSV（放 数据/输入/，自动选最新），或命令行指定：py main.py <输入CSV路径>
    每条记录需含许可证字段（license_text / license / 原许可证 / 原始许可证），其余字段原样保留。

【输出目录】
- 数据/输出/          通过映射/审核的标准化结果 CSV（<stem>_标准化结果.csv）
- 数据/need_check/    需复核的数据（审核未通过）
- 数据/去重表/        去重后的 license_text 表（大模型输入）
- 数据/非标转换数据/  llm_scan 标准化结果（JSONL）
- 数据/非标审核数据/  verify_result 审核结果（JSONL）
- 数据/日志数据/      运行日志 pipeline.log

【依赖】
- module/ 下的 map_csv / map_json / llm_scan / verify_result / data_tool / call_llm_langchain
- tool_calling/ 下的搜索工具（依赖 数据/知识库数据 与 sentence-transformers）
- .env 里的 LLM API 配置（base_url / api_key / model）

【用法】
    py main.py                    # 自动选 数据/输入 下最新的 CSV，跑完整流程
    py main.py <输入CSV路径>       # 命令行指定输入 CSV
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# ── HuggingFace 国内镜像 ──
# 服务器直连 huggingface.co 会被墙，统一走 hf-mirror.com 镜像。
# 必须在 import module（会间接 import sentence-transformers 并读取 HF_ENDPOINT）之前设置。
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
# ── 数值库线程数限制（避免 libgomp「Thread creation failed」/ 抢占过多 CPU）──
# 必须在 import 数值库（numpy/scipy/sklearn/torch，由 module 间接引入）之前设置。
# 值：1 最稳、4 平衡（推荐）、8 更快；按需改。
_NUM_THREADS = "4"
os.environ.setdefault("OMP_NUM_THREADS", _NUM_THREADS)
os.environ.setdefault("MKL_NUM_THREADS", _NUM_THREADS)
os.environ.setdefault("OPENBLAS_NUM_THREADS", _NUM_THREADS)
os.environ.setdefault("NUMEXPR_NUM_THREADS", _NUM_THREADS)
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", _NUM_THREADS)
# 让主程序能 import module 里的模块
sys.path.insert(0, str(Path(__file__).resolve().parent / "module"))

from module import llm_scan
from module import verify_result
from module import map_csv
from module import map_json

# ═══════════════════════════════════════════════════════════════════
# ─── 全局配置（按需修改） ───
# ═══════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent

# ── 输入 ──
INPUT_FILE = None    # 指定输入 CSV（原始数据）路径；None = 自动选 数据/输入 下最新的文件

# ── 输出目录（文件名在运行时根据输入 CSV 推导）──
SCAN_DIR = BASE_DIR / "数据" / "非标转换数据"      # llm_scan 转化结果输出目录
VERIFY_DIR = BASE_DIR / "数据" / "非标审核数据"    # 审核结果输出目录

# ── 非标转化模块（llm_scan.py）──
SCAN_MODEL = 'deepseek-flash'                    # 非标转化模型（None = 用 .env 里 LLM_TECO_MODEL）
SCAN_PROVIDER = "deepseek"              # 非标转化 provider（deepseek/local/qwen/teco）
SCAN_CONCURRENCY = 4               # 转化并发数

# ── 审核模块（verify_result.py）──
VERIFY_MODEL = 'deepseek-flash'                 # 审核模型（None = 用 .env 里 LLM_TECO_MODEL）
VERIFY_PROVIDER = "deepseek"            # 审核 provider（deepseek/local/qwen/teco）
VERIFY_CONCURRENCY = 10              # 审核并发数

# ── 日志记录 ──
LOG_FILE = BASE_DIR / "数据" / "日志数据" / "pipeline.log"  # 日志文件路径
# ═══════════════════════════════════════════════════════════════════


def _setup_logging(log_file: Path) -> None:
    """配置日志：只写入日志文件，不打印到控制台。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",

        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    # 屏蔽第三方库的冗余日志（httpx/openai 的 HTTP 请求日志等）
    for name in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def run_pipeline() -> None:
    # 确保日志目录存在
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _setup_logging(LOG_FILE)

    logging.info("=" * 60)
    logging.info("非标许可证转换 + 审核全流程开始（人工审核前自动跑完）")
    logging.info("非标转化模型: %s（并发 %d）", SCAN_MODEL or "(env 默认)", SCAN_CONCURRENCY)
    logging.info("审核模型: %s（%s，并发 %d）", VERIFY_MODEL, VERIFY_PROVIDER, VERIFY_CONCURRENCY)

    # 1. 第一步：字典映射 + 去重（map_csv）
    logging.info("[1/4] map_csv 字典映射 + 去重...")
    info = map_csv.run_map(INPUT_FILE)
    dedup_file = Path(info["dedup_json"])

    scan_output = SCAN_DIR / (dedup_file.stem + "_output.json")
    verify_output = VERIFY_DIR / (dedup_file.stem + "_verified.json")

    for d in (SCAN_DIR, VERIFY_DIR):
        d.mkdir(parents=True, exist_ok=True)

    if not dedup_file.exists():
        logging.error("去重表不存在: %s", dedup_file)
        return

    # 2. 第二步：llm_scan 标准化
    logging.info("[2/4] llm_scan 非标转化（输入 = %s）...", dedup_file)
    asyncio.run(llm_scan.batch_scan(
        input_path=str(dedup_file),
        output_path=str(scan_output),
        model=SCAN_MODEL,
        provider=SCAN_PROVIDER,
        concurrency=SCAN_CONCURRENCY,
        test_mode=False,
    ))

    # 3. 第三步：verify_result 审核
    logging.info("[3/4] verify_result 审核...")
    asyncio.run(verify_result.verify_batch(
        str(scan_output),
        str(verify_output),
        model=VERIFY_MODEL,
        provider=VERIFY_PROVIDER,
        concurrency=VERIFY_CONCURRENCY,
    ))

    # 4. 第四步：map_json 回填 need_check
    logging.info("[4/4] map_json 回填 need_check...")
    map_json.run_map_json(str(verify_output))

    logging.info("流程完成（人工审核前）。剩余 need_check 待人工审核，之后运行 fill_need_check.py 回填。")


if __name__ == "__main__":
    # 命令行参数可覆盖输入 CSV（原始数据）
    if len(sys.argv) > 1:
        INPUT_FILE = Path(sys.argv[1])
    run_pipeline()
