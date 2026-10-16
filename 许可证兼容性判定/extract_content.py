"""从开源许可证原文中提取 14 个维度的合规信息（基于大模型）。

用法：
    py extract_content.py                    # 批量处理 INPUT_DIR 下所有 .txt，结果写入 OUTPUT_DIR
    py extract_content.py <单个 .txt 文件>    # 只处理指定的一个许可证原文文件
    py extract_content.py -v                 # -v 输出 DEBUG 级详细日志

数据源：
    许可证兼容性判定/许可证原文数据/ 下的 .txt 文件（每份一个许可证原文）

输出：
    许可证兼容性判定/多维度提取结果/<文件名>.json，每份 14 个维度的结构化结果

核心函数：
    await extract_content(license_text) -> list[dict]   # 返回 14 个维度的结构化结果
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

# 切到项目根目录，确保 call_llm_langchain 里的 load_dotenv() 能找到 .env
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from call_llm_langchain import call_llm_api  # noqa: E402

# ──── 全局配置（按需修改） ────
PROMPT_FILE = BASE_DIR / "extract_content_prompt"
INPUT_DIR = BASE_DIR / "许可证原文数据"           # 输入目录：存放许可证原文 .txt 文件
OUTPUT_DIR = BASE_DIR / "多维度合并结果"          # 输出目录：每个许可证一份 14 维度 JSON
LOG_FILE = BASE_DIR / "extract_content.log"       # 日志文件（控制台也会同步输出）

PROVIDER = "remote"     # 见 .env：remote(DeepSeek) / local(qwen3.6-27b) / qwen(阿里云 qwen3.7-plus)
MODEL = "deepseek-v4-pro"            # None = 使用 .env 中的默认模型
TEMPERATURE = 0.0       # 提取任务用 0 保证确定性
MAX_TOKENS = 8000
TOP_P = 0.95
LOG_LEVEL = "INFO"      # 控制台日志级别：DEBUG / INFO / WARNING / ERROR
CONCURRENCY = 8         # 并发数（同时调 LLM 的任务数）

# 14 个维度（固定顺序与名称，与提示词保持一致）
DIMENSIONS = [
    "1.商业使用",
    "2.内部使用",
    "3.分发代码",
    "4.合并/修改代码（代码级传染性）",
    "5.使用库/链接（链接级传染性）",
    "6.专利授权条款",
    "7.许可证变更（再许可）条款",
    "8.网络服务与交互义务",
    "9.归属声明（NOTICE文件）",
    "10.分发与源代码公开",
    "11.商标使用",
    "12.分发与版权声明",
    "13.质量保证",
    "14.附加限制",
]
VALID_STATUS = {"明确允许", "明确禁止", "未涉及", "模糊"}

logger = logging.getLogger("extract_content")


def setup_logging(level: str = LOG_LEVEL, log_file: Path | None = LOG_FILE) -> None:
    """配置日志：同时输出到控制台（stderr）和日志文件。"""
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
    """读取提示词模板。"""
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_messages(license_text: str) -> list[dict]:
    """把许可证原文填入提示词，拆成 system + user 两条消息。"""
    template = load_prompt()
    system_part = template.split("{{license_text}}", 1)[0]
    return [
        {"role": "system", "content": system_part},
        {"role": "user", "content": license_text},
    ]


def _response_text(response) -> str:
    """从 LangChain 返回的响应里取出纯文本内容。"""
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


def _usage_text(response) -> str:
    """从响应里提取 token 用量（若可用）。"""
    um = getattr(response, "usage_metadata", {}) or {}
    if um:
        return f"prompt_tokens={um.get('input_tokens', '?')}, completion_tokens={um.get('output_tokens', '?')}"
    # 兜底：从响应对象其他属性找
    usage = getattr(response, "response_metadata", {}).get("token_usage", {}) or {}
    if usage:
        return f"prompt_tokens={usage.get('prompt_tokens', '?')}, completion_tokens={usage.get('completion_tokens', '?')}"
    return "无 token 信息"


def extract_json(text: str):
    """从 LLM 原始输出中提取 JSON 数组，兼容 markdown 代码块包裹。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = t.find("[")
        end = t.rfind("]")
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise


def validate_result(result) -> list[dict]:
    """对结果做轻量校验：必须是列表，维度数量、状态值做提示（不阻断）。"""
    if not isinstance(result, list):
        raise ValueError(f"模型输出不是 JSON 数组，实际是 {type(result).__name__}")
    if len(result) != len(DIMENSIONS):
        logger.warning("维度数量不符：期望 %d，实际 %d", len(DIMENSIONS), len(result))
    for item in result:
        if not isinstance(item, dict):
            raise ValueError("维度项必须是 JSON 对象")
        if item.get("状态") not in VALID_STATUS:
            logger.warning("非法状态值：%r -> %r", item.get("维度", "?"), item.get("状态"))
    return result


async def extract_content(license_text: str, model: str | None = None,
                          provider: str | None = None) -> list[dict]:
    """提取一份许可证原文的 14 个维度信息。

    参数：
        license_text: 许可证原文文本
        model / provider: 可覆盖全局配置；不传则用全局配置
    返回：
        14 个维度对象的列表，每个对象含 维度 / 状态 / 原文 / 解释 四个字段
    """
    if not license_text or not license_text.strip():
        raise ValueError("许可证原文为空")

    used_model = model or MODEL or "(env 默认)"
    used_provider = provider or PROVIDER
    logger.info("开始提取：输入 %d 字 | provider=%s | model=%s", len(license_text), used_provider, used_model)

    messages = build_messages(license_text)
    logger.debug("消息已构建：system=%d 字, user=%d 字",
                 len(messages[0]["content"]), len(messages[1]["content"]))

    t0 = time.perf_counter()
    response = await call_llm_api(
        messages,
        model=model or MODEL,
        provider=provider or PROVIDER,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        top_p=TOP_P,
    )
    elapsed = time.perf_counter() - t0
    logger.info("LLM 调用完成：耗时 %.2fs | %s", elapsed, _usage_text(response))

    raw = _response_text(response)
    logger.debug("原始输出（前 600 字）：\n%s", raw[:600])

    result = extract_json(raw)
    logger.info("JSON 解析成功：%d 个维度", len(result))
    result = validate_result(result)

    for item in result:
        logger.debug("  [%s] %s", item.get("维度", "?"), item.get("状态", "?"))
    return result


def _list_txt_files(directory: Path) -> list[Path]:
    """列出目录下所有 .txt 许可证原文文件（按文件名排序）。"""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.txt"))


def _is_valid_result(path: Path) -> bool:
    """检查已有结果文件是否为合法、非空的 JSON 数组。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, list) and len(data) > 0
    except Exception:
        return False


async def main() -> None:
    args = sys.argv[1:]
    verbose = "-v" in args or "--verbose" in args
    args = [a for a in args if a not in ("-v", "--verbose")]
    setup_logging("DEBUG" if verbose else LOG_LEVEL)

    logger.info("=" * 60)
    logger.info("extract_content 启动")
    logger.info("提示词文件: %s", PROMPT_FILE)
    logger.info("provider=%s | model=%s | temperature=%s | max_tokens=%s",
                PROVIDER, MODEL or "(env 默认)", TEMPERATURE, MAX_TOKENS)

    # 确定输入 .txt 文件列表：指定单个文件 > 指定目录 > 默认 INPUT_DIR
    if args:
        p = Path(args[0])
        if p.is_file():
            txt_files = [p]
        elif p.is_dir():
            txt_files = _list_txt_files(p)
        else:
            logger.error("输入路径不存在: %s", p)
            return
    else:
        txt_files = _list_txt_files(INPUT_DIR)

    if not txt_files:
        logger.error("未找到任何许可证原文 .txt 文件（输入目录: %s）", INPUT_DIR)
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("待处理 .txt: %d 个", len(txt_files))
    logger.info("输出目录: %s", OUTPUT_DIR)

    # 预筛选：已有合法结果的跳过（存在但损坏/为空的会重新生成）
    pending = []  # (序号, 输入文件, 输出文件)
    skip = 0
    for i, txt_path in enumerate(txt_files, 1):
        out_path = OUTPUT_DIR / (txt_path.stem + ".json")
        if out_path.exists() and _is_valid_result(out_path):
            logger.info("[%d/%d] 已存在，跳过: %s", i, len(txt_files), out_path.name)
            skip += 1
            continue
        pending.append((i, txt_path, out_path))

    logger.info("本次需处理 %d 个，并发数 %d", len(pending), CONCURRENCY)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def _process(i, txt_path, out_path):
        text = txt_path.read_text(encoding="utf-8")
        if not text.strip():
            logger.warning("[%d/%d] 空文本，跳过: %s", i, len(txt_files), txt_path.name)
            return "skip"
        t0 = time.perf_counter()
        try:
            async with sem:
                result = await extract_content(text)
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("[%d/%d] 完成 %s -> %s，耗时 %.2fs",
                        i, len(txt_files), txt_path.name, out_path.name, time.perf_counter() - t0)
            return "ok"
        except Exception as e:
            logger.exception("[%d/%d] 失败 %s: %s", i, len(txt_files), txt_path.name, e)
            return "fail"

    results = await asyncio.gather(*(_process(i, p, o) for i, p, o in pending))
    ok = results.count("ok")
    fail = results.count("fail")
    skip += results.count("skip")

    logger.info("处理完成：成功 %d / 跳过 %d / 失败 %d", ok, skip, fail)
    logger.info("结果目录: %s", OUTPUT_DIR)


if __name__ == "__main__":
    asyncio.run(main())
