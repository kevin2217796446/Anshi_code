"""判断两个许可证之间的兼容性（基于大模型）。

用法：
    py justify_com.py            # 批量处理所有许可证对，结果写入 OUTPUT_DIR
    py justify_com.py -v         # -v 输出 DEBUG 级详细日志
    py justify_com.py --only-dir 待重跑目录   # 只重跑该目录里文件名（原__目标.json）对应的对

数据源：
    许可证兼容性判定/多维度提取结果/ 下的 .json 文件（每个许可证的 14 维度分析）

输出：
    许可证兼容性判定/兼容性判定结果/{原}__{目标}.json，每个有序对一份判定结果

核心函数：
    await justify_pair(orig_name, orig_json, target_name, target_json) -> dict
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
PROMPT_FILE = BASE_DIR / "justify_com_prompt"
INPUT_DIR = BASE_DIR / "多维度合并结果"         # 输入：每个许可证的 14 维度 JSON
OUTPUT_DIR = BASE_DIR / "兼容性判定结果" /"测试结果_合并11"         # 输出：每个有序对一份判定 JSON
LOG_FILE = BASE_DIR / "justify_com.log"

PROVIDER = "remote"     # 见 .env：remote(DeepSeek) / local(qwen3.6-27b) / qwen(阿里云 qwen3.7-plus)
MODEL = "deepseek-v4-flash"            # None = 使用 .env 中的默认模型
TEMPERATURE = 0.0       # 判定任务用 0 保证确定性
MAX_TOKENS = 100000
TOP_P = 0.95
LOG_LEVEL = "INFO"      # 控制台日志级别：DEBUG / INFO / WARNING / ERROR
CONCURRENCY = 50        # 并发数（同时调 LLM 的任务数）

logger = logging.getLogger("justify_com")


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


def build_messages(orig_name: str, orig_json, target_name: str, target_json) -> list[dict]:
    """把两个许可证的名称 + 14 维度 JSON 填进提示词，拆成 system + user。"""
    template = load_prompt()
    marker = "【输入数据】"
    if marker in template:
        system_part = template.split(marker, 1)[0].strip()
    else:
        system_part = template.strip()

    user_content = (
        f"原许可证:\n{orig_name}\n{json.dumps(orig_json, ensure_ascii=False, indent=2)}\n\n"
        f"目标许可证:\n{target_name}\n{json.dumps(target_json, ensure_ascii=False, indent=2)}"
    )
    return [
        {"role": "system", "content": system_part},
        {"role": "user", "content": user_content},
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
    usage = getattr(response, "response_metadata", {}).get("token_usage", {}) or {}
    if usage:
        return f"prompt_tokens={usage.get('prompt_tokens', '?')}, completion_tokens={usage.get('completion_tokens', '?')}"
    return "无 token 信息"


def _usage_dict(response) -> dict:
    """从响应里提取 token 用量 dict（若可用，缺失键返回 None）。"""
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


def _response_model(response) -> str:
    """从响应元数据里提取实际返回的模型名（若可用）。"""
    rm = getattr(response, "response_metadata", {}) or {}
    return rm.get("model_name", "") or rm.get("model", "") or ""


def extract_json(text: str):
    """从 LLM 原始输出中提取 JSON 对象，兼容 markdown 代码块包裹。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # 提取第一个 { 到最后一个 }
        start = t.find("{")
        end = t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise


def _normalize_result(result, orig_name: str, target_name: str) -> dict:
    """归一化判定结果：强制名称用已知值，兼容性转 int。"""
    if not isinstance(result, dict):
        raise ValueError(f"模型输出不是 JSON 对象，实际是 {type(result).__name__}")
    result["原许可证"] = orig_name
    result["目标许可证"] = target_name
    compat = result.get("兼容性")
    if isinstance(compat, str):
        try:
            result["兼容性"] = int(compat)
        except ValueError:
            pass
    if result.get("兼容性") not in (-1, 0, 1):
        logger.warning("兼容性值应为 -1/0/1，实际 %r", result.get("兼容性"))
    # 所有兼容性取值都应带依据；为空时告警（便于发现模型漏写）
    if not str(result.get("依据", "")).strip():
        logger.warning("依据为空：%s -> %s（兼容性=%s）", orig_name, target_name, result.get("兼容性"))
    return result


async def justify_pair(orig_name: str, orig_json, target_name: str, target_json,
                       model: str | None = None, provider: str | None = None) -> tuple[dict, dict]:
    """判断一对许可证的兼容性（原许可证 + 目标许可证）。

    返回：
        (result, usage)
        result = {"原许可证": ..., "目标许可证": ..., "兼容性": 0/1/-1, "依据": ...}
        usage  = {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}
    """
    messages = build_messages(orig_name, orig_json, target_name, target_json)

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

    actual_model = _response_model(response)
    logger.info("模型调用：请求=%s（provider=%s）| 实际返回=%s",
                used_model, used_provider, actual_model or "未知")

    raw = _response_text(response)
    logger.debug("原始输出（前 600 字）：\n%s", raw[:600])

    result = extract_json(raw)
    result = _normalize_result(result, orig_name, target_name)
    return result, _usage_dict(response)


def _list_json_files(directory: Path) -> list[Path]:
    """列出目录下所有 .json 文件（按文件名排序）。"""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def _is_valid_result(path: Path) -> bool:
    """检查已有结果文件是否为合法 JSON 对象。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(data, dict)
    except Exception:
        return False


def _collect_done(output_dir: Path) -> set[str]:
    """递归收集输出目录下已完成的结果文件名（stem），只统计合法 JSON 对象。

    会扫描 output_dir 及其所有子文件夹（如 兼容/待定/不兼容），
    这样已分类到子文件夹里的结果也能被识别，避免重复跑。
    """
    done = set()
    if not output_dir.is_dir():
        return done
    for f in output_dir.rglob("*.json"):
        if f.is_file() and _is_valid_result(f):
            done.add(f.stem)
    return done


async def main() -> None:
    args = sys.argv[1:]
    verbose = "-v" in args or "--verbose" in args
    setup_logging("DEBUG" if verbose else LOG_LEVEL)

    # 解析 --only-dir：只重跑该目录下文件名（原__目标.json）对应的许可证对
    # 相对路径以脚本所在目录（许可证兼容性判定）为基准
    only_dir = None
    for i, a in enumerate(args):
        if a in ("--only-dir", "--retry-dir") and i + 1 < len(args):
            only_dir = Path(args[i + 1])
            if not only_dir.is_absolute():
                only_dir = BASE_DIR / only_dir
            break

    logger.info("=" * 60)
    logger.info("justify_com 启动")
    logger.info("提示词文件: %s", PROMPT_FILE)
    logger.info("provider=%s | model=%s | concurrency=%d | max_tokens=%s",
                PROVIDER, MODEL or "(env 默认)", CONCURRENCY, MAX_TOKENS)

    # 加载所有许可证的 14 维度 JSON
    licenses: dict[str, list] = {}
    for f in _list_json_files(INPUT_DIR):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("跳过无法解析的文件 %s: %s", f.name, e)
            continue
        if isinstance(data, list):
            licenses[f.stem] = data

    if not licenses:
        logger.error("未在 %s 找到任何有效的许可证 JSON", INPUT_DIR)
        return

    names = sorted(licenses.keys())
    logger.info("加载 %d 个许可证：%s", len(names), ", ".join(names[:5]) + ("..." if len(names) > 5 else ""))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("输出目录: %s", OUTPUT_DIR)

    if only_dir is not None:
        # --only-dir 模式：只重跑该目录里文件名对应的对，强制重跑（不跳过已存在的）
        if not only_dir.is_dir():
            logger.error("--only-dir 目录不存在: %s", only_dir)
            return
        pairs = []
        for f in sorted(only_dir.glob("*.json")):
            if "__" not in f.stem:
                continue
            orig, target = f.stem.split("__", 1)
            if orig in licenses and target in licenses:
                pairs.append((orig, target))
            else:
                logger.warning("跳过 %s：许可证不在输入中", f.name)
        # 支持断点续跑：已重跑过的（结果已在 测试结果/ 里）跳过
        done = _collect_done(OUTPUT_DIR)
        logger.info("--only-dir 模式：%s 中共 %d 对，其中 %d 对已重跑可跳过",
                    only_dir, len(pairs), len({f"{o}__{t}" for o, t in pairs} & done))
    else:
        # 枚举有序对（每个许可证既当原、也当目标，排除自身）
        pairs = [(o, t) for o in names for t in names if o != t]
        logger.info("共 %d 个有序对（%d × %d，双向）", len(pairs), len(names), len(names))
        # 预筛选：递归扫描输出目录（含 兼容/待定/不兼容 子文件夹），已有结果的跳过
        done = _collect_done(OUTPUT_DIR)

    pending = []
    skip = 0
    for orig, target in pairs:
        key = f"{orig}__{target}"
        if key in done:
            skip += 1
            continue
        pending.append((orig, target, OUTPUT_DIR / f"{key}.json"))

    logger.info("本次需处理 %d 对，跳过 %d 对（含子文件夹中已存在的）", len(pending), skip)

    t_start = time.perf_counter()
    sem = asyncio.Semaphore(CONCURRENCY)
    total_prompt = total_completion = 0  # 累计 token
    compat_counts = {1: 0, 0: 0, -1: 0}  # 兼容性 -> 数量
    incompatible = []  # 不兼容（兼容性=1）的 (原, 目标, 依据)

    async def _process(orig, target, out_path):
        nonlocal total_prompt, total_completion
        t0 = time.perf_counter()
        try:
            async with sem:
                result, usage = await justify_pair(orig, licenses[orig], target, licenses[target])
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            pt = usage.get("prompt_tokens") or 0
            ct = usage.get("completion_tokens") or 0
            total_prompt += pt
            total_completion += ct
            compat = result.get("兼容性")
            if compat in compat_counts:
                compat_counts[compat] += 1
            if compat == 1:
                incompatible.append((orig, target, result.get("依据", "")))
            logger.info("完成 %s -> %s：兼容性=%s | 耗时 %.2fs | prompt_tokens=%s, completion_tokens=%s",
                        orig, target, compat, time.perf_counter() - t0, pt, ct)
            return "ok"
        except Exception as e:
            logger.exception("失败 %s -> %s: %s", orig, target, e)
            return "fail"

    results = await asyncio.gather(*(_process(o, t, p) for o, t, p in pending))
    ok = results.count("ok")
    fail = results.count("fail")
    total_time = time.perf_counter() - t_start

    logger.info("处理完成：成功 %d / 失败 %d / 跳过 %d", ok, fail, skip)
    logger.info("累计 token：prompt=%d, completion=%d, total=%d",
                total_prompt, total_completion, total_prompt + total_completion)
    logger.info("总耗时：%.1fs（%.2f 分钟）", total_time, total_time / 60)
    logger.info("兼容性统计：兼容(0)=%d，不能兼容(1)=%d，视情况(-1)=%d",
                compat_counts.get(0, 0), compat_counts.get(1, 0), compat_counts.get(-1, 0))

    if incompatible:
        logger.info("=" * 60)
        logger.info("不兼容的结果（共 %d 对）：", len(incompatible))
        for orig, target, reason in sorted(incompatible):
            brief = (reason or "").replace("\n", " ").strip()
            logger.info("  %s -> %s：%s", orig, target, brief[:120])
    else:
        logger.info("无不兼容结果")

    logger.info("结果目录: %s", OUTPUT_DIR)


if __name__ == "__main__":
    asyncio.run(main())
