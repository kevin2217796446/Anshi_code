"""清洗兼容性判定结果中的「依据」字段：去掉内部术语（维度等），面向外部客户。

用法：
    py clean_yiju.py            # 批量处理 测试结果_合并 下所有 .json，结果写入 OUTPUT_DIR
    py clean_yiju.py -v         # -v 输出 DEBUG 级详细日志

数据源：
    兼容性判定结果/测试结果_合并/ 下的 .json（每个含 原许可证/目标许可证/兼容性/依据）

输出：
    兼容性判定结果/测试结果_合并_客户版/<原>__<目标>.json（依据已清洗）
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# ──── 路径与运行环境 ────
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from call_llm_langchain import call_llm_api  # noqa: E402

# ──── 全局配置（按需修改） ────
PROMPT_FILE = BASE_DIR / "clean_yiju_prompt"
INPUT_DIR = BASE_DIR / "兼容性判定结果" / "测试结果_合并"
OUTPUT_DIR = BASE_DIR / "兼容性判定结果" / "测试结果_合并_客户版"
LOG_FILE = BASE_DIR / "clean_yiju.log"

PROVIDER = "remote"           # remote(DeepSeek) / local / qwen
MODEL = "deepseek-v4-flash"   # None = 用 .env 默认模型
TEMPERATURE = 0.0
MAX_TOKENS = 30000
TOP_P = 0.95
LOG_LEVEL = "INFO"
CONCURRENCY = 20

logger = logging.getLogger("clean_yiju")


def setup_logging(level: str = LOG_LEVEL, log_file: Path | None = LOG_FILE) -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(lvl)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            logger.warning("无法写入日志文件 %s: %s", log_file, e)


def load_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_messages(record: dict) -> list[dict]:
    template = load_prompt()
    user_content = json.dumps(record, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": template},
        {"role": "user", "content": user_content},
    ]


def _response_text(response) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def _usage_dict(response) -> dict:
    um = getattr(response, "usage_metadata", {}) or {}
    if um:
        return {
            "prompt_tokens": um.get("input_tokens"),
            "completion_tokens": um.get("output_tokens"),
            "total_tokens": um.get("total_tokens"),
        }
    usage = getattr(response, "response_metadata", {}).get("token_usage", {}) or {}
    if usage:
        return {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    return {}


def extract_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = t.find("{")
        end = t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise


def _normalize_result(result, orig: str, target: str, compat) -> dict:
    """强制用输入的 原许可证/目标许可证/兼容性，只取清洗后的 依据。"""
    if not isinstance(result, dict):
        raise ValueError("模型输出不是 JSON 对象，实际是 " + type(result).__name__)
    yiju = str(result.get("依据", "")).strip()
    if not yiju:
        raise ValueError("模型返回的依据为空")
    if "维度" in yiju:
        logger.warning("清洗后仍含「维度」: %s -> %s", orig, target)
    return {
        "原许可证": orig,
        "目标许可证": target,
        "兼容性": compat,
        "依据": yiju,
    }


async def clean_one(record: dict, model: str | None = None,
                    provider: str | None = None) -> tuple[dict, dict]:
    messages = build_messages(record)
    used_model = model or MODEL
    used_provider = provider or PROVIDER
    response = await call_llm_api(
        messages,
        model=used_model,
        provider=used_provider,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        top_p=TOP_P,
    )
    raw = _response_text(response)
    logger.debug("原始输出（前 500 字）：\n%s", raw[:500])
    result = extract_json(raw)
    result = _normalize_result(result, record.get("原许可证", ""),
                               record.get("目标许可证", ""), record.get("兼容性"))
    return result, _usage_dict(response)


def _is_valid_result(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict) and str(data.get("依据", "")).strip()
    except Exception:
        return False


async def main() -> None:
    args = sys.argv[1:]
    verbose = "-v" in args or "--verbose" in args
    no_resume = "--no-resume" in args
    setup_logging("DEBUG" if verbose else LOG_LEVEL)

    logger.info("=" * 60)
    logger.info("clean_yiju 启动")
    logger.info("提示词文件: %s", PROMPT_FILE)
    logger.info("provider=%s | model=%s | concurrency=%d | max_tokens=%s",
                PROVIDER, MODEL or "(env 默认)", CONCURRENCY, MAX_TOKENS)

    if not INPUT_DIR.is_dir():
        logger.error("输入目录不存在: %s", INPUT_DIR)
        return

    files = sorted(INPUT_DIR.glob("*.json"))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("输入: %s（%d 个文件）", INPUT_DIR, len(files))
    logger.info("输出: %s", OUTPUT_DIR)

    # 预筛选：已有合法结果的跳过（支持断点续跑）；--no-resume 则全部重跑
    pending = []
    skip = 0
    for f in files:
        out_path = OUTPUT_DIR / f.name
        if not no_resume and out_path.exists() and _is_valid_result(out_path):
            skip += 1
            continue
        pending.append(f)

    logger.info("本次需处理 %d 个，跳过 %d 个", len(pending), skip)

    t_start = time.perf_counter()
    sem = asyncio.Semaphore(CONCURRENCY)
    total_prompt = 0
    total_completion = 0
    ok = 0
    fail = 0

    async def _process(f: Path):
        nonlocal total_prompt, total_completion, ok, fail
        t0 = time.perf_counter()
        try:
            record = json.loads(f.read_text(encoding="utf-8"))
            async with sem:
                result, usage = await clean_one(record)
            (OUTPUT_DIR / f.name).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            pt = usage.get("prompt_tokens") or 0
            ct = usage.get("completion_tokens") or 0
            total_prompt += pt
            total_completion += ct
            ok += 1
            logger.info("[%d/%d] 完成 %s（耗时 %.2fs，pt=%s ct=%s）",
                        ok + fail, len(pending), f.name, time.perf_counter() - t0, pt, ct)
            return "ok"
        except Exception as e:
            fail += 1
            logger.exception("失败 %s: %s", f.name, e)
            return "fail"

    await asyncio.gather(*(_process(f) for f in pending))
    total_time = time.perf_counter() - t_start

    logger.info("处理完成：成功 %d / 失败 %d / 跳过 %d", ok, fail, skip)
    logger.info("累计 token：prompt=%d, completion=%d", total_prompt, total_completion)
    logger.info("总耗时：%.1fs（%.2f 分钟）", total_time, total_time / 60)
    logger.info("结果目录: %s", OUTPUT_DIR)


if __name__ == "__main__":
    asyncio.run(main())
