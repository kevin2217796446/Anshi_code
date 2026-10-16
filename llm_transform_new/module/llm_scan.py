"""异步批量许可证标准化引擎 — 严格遵循 LICENSE_NORMALIZER_SKILL.md 流程。"""

import asyncio
import json
import time
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from call_llm_langchain import _get_llm
from data_tool import _find_json_objects_in_array, record_id, load_done_ids, load_jsonl
from tool_calling import (
    split_license_expression,
    search_similar_licenses,
    classify_license_type,
    validate_spdx_identifier,
    search_spdx,
    validate_scancode_identifier,
    search_scancode,
    validate_exception_identifier,
    search_exception,
    merge_license_result,
    split_with_expression,
)
from tool_calling.search_knowledge import wait_until_loaded
from tool_calling import split_merge_tools

# ──── 全局配置（按需修改） ────
BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "数据" / "0827非标转化" / "license_need_check_20260826.json"       # 输入文件路径
OUTPUT_FILE = BASE_DIR / "数据" / "0827非标转化" / "license_need_check_20260826_output.json"      # 输出文件路径

# ──── 全部工具列表 ────
ALL_TOOLS = [
    split_license_expression,
    search_similar_licenses,
    classify_license_type,
    validate_spdx_identifier,
    search_spdx,
    validate_scancode_identifier,
    search_scancode,
    validate_exception_identifier,
    search_exception,
    merge_license_result,
    split_with_expression,
]

# ──── 系统提示词（对齐 LICENSE_NORMALIZER_SKILL.md）────
def _load_system_prompt() -> str:
    """从 LICENSE_NORMALIZER_SKILL.md 提取核心业务规则和流程步骤。"""
    import re as _re
    raw = (Path(__file__).parent / "LICENSE_NORMALIZER_SKILL.md").read_text(encoding="utf-8")
    rules = _re.search(r"## 核心业务规则（最高优先级）.*?(?=\n## )", raw, _re.DOTALL)
    limits = _re.search(r"## 输出限制.*?(?=\n## )", raw, _re.DOTALL)
    flow = _re.search(r"## 标准执行流程.*?(?=\n## )", raw, _re.DOTALL)
    parts = []
    if rules:
        parts.append(rules.group().strip())
    if limits:
        parts.append(limits.group().strip())
    if flow:
        parts.append(flow.group().strip())
    return "\n\n".join(parts) if parts else raw

SYSTEM_PROMPT = _load_system_prompt()


def _extract_spdx_name(raw: str) -> str:
    """从 LLM 原始输出中提取 license_spdx_name，兼容各种包裹格式。"""
    import re as _re

    text = raw.strip()

    # 去掉 markdown 代码块包裹
    text = _re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
    text = _re.sub(r'\n?```\s*$', '', text.strip())

    # 尝试直接解析 JSON
    try:
        return json.loads(text).get("license_spdx_name", "none")
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试从文本中提取 JSON 对象
    m = _re.search(r'\{[^{}]*"license_spdx_name"\s*:\s*"[^"]*"[^{}]*\}', text)
    if m:
        try:
            return json.loads(m.group()).get("license_spdx_name", "none")
        except (json.JSONDecodeError, TypeError):
            pass

    # 最后回退：去掉引号
    return text.strip().strip('"').strip("'") or "none"


def _response_text(response) -> str:
    """从 LLM 响应里取正文；content 为空时回退到 reasoning_content（Qwen 思考模型）。"""
    content = getattr(response, "content", "")
    if content:
        if isinstance(content, str):
            return content
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    rk = getattr(response, "reasoning_content", None)
    if not rk:
        rk = (getattr(response, "additional_kwargs", {}) or {}).get("reasoning_content", "")
    return str(rk or "")


# ──── 可重试的瞬态错误（503 算力不足 / 429 限流 / 连接超时等）────
try:
    from openai import InternalServerError, RateLimitError, APIConnectionError, APITimeoutError
    _RETRYABLE_EXCEPTIONS = (InternalServerError, RateLimitError, APIConnectionError, APITimeoutError)
except ImportError:
    _RETRYABLE_EXCEPTIONS = ()

try:
    import httpx
    _RETRYABLE_EXCEPTIONS = _RETRYABLE_EXCEPTIONS + (httpx.ConnectError, httpx.TimeoutException)
except ImportError:
    pass


async def _ainvoke_with_retry(llm, messages, max_retries: int = 4, base_delay: float = 2.0):
    """对瞬态错误（503/429/连接/超时）做指数退避重试；重试仍失败则抛出异常。

    延迟序列：base_delay * 2^attempt，即 2s / 4s / 8s（共重试 3 次）。
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await llm.ainvoke(messages)
        except _RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt >= max_retries - 1:
                break
            delay = base_delay * (2 ** attempt)
            print(f"  [重试] {type(e).__name__}: {str(e)[:120]}，{delay:.0f}s 后重试 ({attempt + 1}/{max_retries - 1})", flush=True)
            await asyncio.sleep(delay)
    raise last_exc


async def scan_by_llm(license_text: str, model_name: str | None = None,
                     provider: str | None = None,
                     verbose: bool = False) -> tuple[str, int, int]:
    """按 Skill 流程调用 LLM + 工具完成许可证标准化，返回 (spdx_name, prompt_tokens, completion_tokens)。"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": license_text},
    ]

    if verbose:
        print(f"\n{'='*70}")
        print(f"[VERBOSE] 输入: {license_text[:100]}")
        print(f"{'='*70}")

    try:
        llm = _get_llm(provider=provider or "deepseek", model=model_name)
        llm_with_tools = llm.bind_tools(ALL_TOOLS)

        lc_msgs = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                lc_msgs.append(SystemMessage(content=content))
            else:
                lc_msgs.append(HumanMessage(content=content))

        max_rounds = 50
        total_prompt = 0
        total_completion = 0
        for _round in range(max_rounds):
            response = await _ainvoke_with_retry(llm_with_tools, lc_msgs)
            tool_calls = getattr(response, "tool_calls", []) or []

            # 累计 token
            um = getattr(response, "usage_metadata", {}) or {}
            total_prompt += um.get("input_tokens", 0)
            total_completion += um.get("output_tokens", 0)

            if verbose:
                reasoning = getattr(response, "reasoning", "") or ""
                if reasoning:
                    print(f"\n[LLM 思考 #{_round+1}] {str(reasoning)[:500]}...")
                if response.content:
                    print(f"[LLM 回复 #{_round+1}] {str(response.content)[:300]}")

            # 最终回复
            text = _response_text(response)
            if text and not tool_calls:
                result = _extract_spdx_name(text)
                if verbose:
                    print(f"\n[结果] {result}")
                return result, total_prompt, total_completion

            # tool_calls — 执行并记录
            if tool_calls:
                lc_msgs.append(response)
                for tc in tool_calls:
                    tool_name = tc.get("name", "")
                    tool_args = tc.get("args", {})
                    tool_func = {t.name: t for t in ALL_TOOLS}.get(tool_name)

                    if verbose:
                        print(f"  [工具调用] {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:200]})")

                    if tool_func:
                        try:
                            result = await tool_func.ainvoke(tool_args)
                            from langchain_core.messages import ToolMessage
                            lc_msgs.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
                            if verbose:
                                print(f"  [工具结果] {str(result)[:500]}")
                        except Exception as e:
                            from langchain_core.messages import ToolMessage
                            lc_msgs.append(ToolMessage(content=f"ERROR: {e}", tool_call_id=tc["id"]))
                            if verbose:
                                print(f"  [工具异常] {e}")
                    else:
                        from langchain_core.messages import ToolMessage
                        lc_msgs.append(ToolMessage(content=f"未知工具: {tool_name}", tool_call_id=tc["id"]))
                continue

            print(f"  [WARN] 空响应, round={_round}")
            break

        return "none", total_prompt, total_completion

    except Exception as e:
        # 瞬态错误重试后仍失败：向上抛出，由 batch_scan 跳过不落盘（下次续跑重试），
        # 避免错误地把「转换失败」当成「非许可证(none)」写进结果。
        import traceback
        print(f"  [ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        raise


def load_records_from_json(file_path: str | Path) -> list[dict]:
    """读取 JSON 文件，返回完整记录列表（保留 purl/version/license_text 等字段）。

    支持格式：
    - JSON 数组: [{"purl": ..., "version": ..., "license_text": ...}, ...]
    - {"RECORDS": [...]} / 嵌套对象
    - JSONL：每行一个 JSON 对象
    - 截断/损坏的 JSON：自动提取匹配的对象
    """
    file_path = Path(file_path)
    raw = file_path.read_text(encoding="utf-8").strip()
    records: list[dict] = []

    # 格式 1：JSON 数组 [...]
    if raw.startswith("["):
        try:
            records = json.loads(raw)
        except json.JSONDecodeError:
            records = []

    # 格式 2：{"RECORDS": [...]} 或单个对象
    if not records and raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for val in obj.values():
                    if isinstance(val, list):
                        records = val
                        break
                if not records:
                    records = [obj]
        except json.JSONDecodeError:
            # JSON 损坏：定位数组并逐对象提取
            for chunk in _find_json_objects_in_array(raw):
                try:
                    records.append(json.loads(chunk))
                except json.JSONDecodeError:
                    pass

    # 格式 3：JSONL，每行一个对象
    if not records:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line in ("{", "}", "[", "]", "{,", "[,"):
                continue
            if line.endswith(","):
                line = line[:-1]
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    return records


def _license_text_of(record: dict) -> str:
    """从记录中提取待转换的许可证文本：优先 license_text，回退 license / 原许可证 / 原始许可证。"""
    for field in ("license_text", "license", "原许可证", "原始许可证"):
        val = record.get(field)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


async def batch_scan(
    input_path: str,
    output_path: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    concurrency: int = 10,
    test_mode: bool = False,
    limit: int = 0,  # 只测前 N 条，0=全部
    verify: bool = False,  # 结果生成后用 LLM 验证可信度
):
    """异步批量处理：读取 JSON -> 并发 LLM 标准化 -> 写入 JSON。test_mode=True 时生成测试报告。"""
    split_merge_tools.set_split_provider(provider or "deepseek")
    split_merge_tools.set_split_model(model)
    input_path = Path(input_path)
    if output_path is None:
        output_path = Path(__file__).parent / BASE_DIR / "llm_scan_smalltest_output.json"
    else:
        output_path = Path(output_path)

    if not input_path.exists():
        print(f"文件不存在: {input_path}")
        return

    records = load_records_from_json(input_path)
    if limit and limit < len(records):
        records = records[:limit]
    total = len(records)

    if total == 0:
        print("数据集为空，退出。")
        return

    # 断点续跑：每条记录的 record_id + 已完成的集合
    rids = [record_id(r) for r in records]
    done = load_done_ids(output_path)
    todo = [i for i in range(total) if rids[i] not in done]
    todo_count = len(todo)

    # 加载 ground truth（仅测试模式）
    ground_truth = {}
    if test_mode:
        test_file = Path(__file__).parent /"数据"/"全量跑"/"最近一次的结果.json"
        if test_file.exists():
            gt_raw = json.loads(test_file.read_text(encoding="utf-8"))
            gt_list = gt_raw.get("RECORDS", []) if isinstance(gt_raw, dict) else gt_raw
            for g in gt_list:
                ground_truth[g["license"]] = g.get("license_spdx_name", "")
            print(f"Ground Truth: {len(ground_truth)} 条")
        else:
            print("未找到 test_dataset_half1.json，跳过比对")
            test_mode = False

    used_model = model or "默认"
    print(f"输入: {input_path}  ({total} 条，待处理 {todo_count} 条)")
    print(f"输出: {output_path}")
    print(f"模型: {used_model}")
    print(f"并发: {concurrency}")
    print(f"测试模式: {'ON' if test_mode else 'OFF'}")
    print("-" * 60)

    if todo_count == 0:
        print("全部已处理，无需重跑。")
        return

    print("预热向量模型...")
    wait_until_loaded()
    print("就绪\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    t_start = time.perf_counter()
    completed = 0
    failed = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    sem = asyncio.Semaphore(concurrency)

    async def process_one(i: int):
        record = records[i]
        text = _license_text_of(record)
        if not text:
            print(f"  [{i+1}/{total}] 跳过空许可证 -> ''")
            return i, "", 0, 0
        t0 = time.perf_counter()
        async with sem:
            try:
                result, pt, ct = await scan_by_llm(text, model_name=model, provider=provider, verbose=(limit > 0))
            except Exception:
                # 转换失败（重试后仍失败）：返回 None 标记，不落盘，下次续跑重试
                print(f"  [{i+1}/{total}] 转换失败(跳过，下次续跑重试): {text[:50]!r}")
                return i, None, 0, 0
        elapsed = (time.perf_counter() - t0) * 1000
        match = ""
        if test_mode and ground_truth:
            expected = ground_truth.get(text, "")
            match = " [OK]" if result == expected else f" [FAIL exp={expected[:30]}]"
        print(f"  [{i+1}/{total}] {elapsed:.0f}ms {text[:50]!r} -> {result}{match}")
        return i, result, pt, ct

    async def progress_reporter():
        while completed < todo_count:
            await asyncio.sleep(30)
            elapsed = time.perf_counter() - t_start
            print(f"  进度: {completed}/{todo_count} ({completed/todo_count*100:.0f}%) | {elapsed:.0f}s")

    tasks = [process_one(i) for i in todo]
    reporter = asyncio.create_task(progress_reporter())

    # 逐条落盘（append + line buffered，中断可续跑）
    with output_path.open("a", encoding="utf-8", buffering=1) as out:
        for coro in asyncio.as_completed(tasks):
            i, result, pt, ct = await coro
            if result is None:
                # 失败记录不落盘，避免被断点续跑误判为已完成
                failed += 1
                continue
            rec = dict(records[i])          # 浅拷贝，保留所有原始字段
            rec["record_id"] = rids[i]
            rec["license_spdx_name"] = result
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            total_prompt_tokens += pt
            total_completion_tokens += ct
            completed += 1

    reporter.cancel()
    total_time = time.perf_counter() - t_start
    print("-" * 60)
    print(f"本次完成: {completed}/{todo_count} 条 | 失败 {failed} 条 | {total_time:.1f}s")
    print(f"Token: prompt={total_prompt_tokens} completion={total_completion_tokens}")
    print(f"结果: {output_path}")

    # 测试报告：读回全量 JSONL（含续跑部分），创建子文件夹存放结果和日志
    if test_mode and ground_truth:
        from datetime import datetime
        safe_model = used_model.replace("/", "_").replace(":", "_")
        folder_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_model}"
        test_folder = Path(__file__).parent / "数据" / "模型测试" / folder_name
        test_folder.mkdir(parents=True, exist_ok=True)

        output = load_jsonl(output_path)
        rid_to_idx = {rid: idx for idx, rid in enumerate(rids)}
        output.sort(key=lambda r: rid_to_idx.get(r.get("record_id", ""), len(rids)))

        # 测试输出直接用主输出（已含 license / license_spdx_name），供 generate_log / verify_batch 使用
        test_output_path = test_folder / "test_output.json"
        test_output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

        from record_test_log import generate_log
        generate_log(
            results=output,
            ground_truth=ground_truth,
            model=used_model,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_time=total_time,
            output_dir=test_folder,
        )
        print(f"测试文件夹: {test_folder}")

        # 自动验证（可选）
        if verify:
            print("\n启动 LLM 可信度验证...")
            from verify_result import verify_batch
            await verify_batch(str(test_output_path), str(test_folder))
            print("验证完成")

    elif verify:
        print("⚠ verify=True 仅在 test_mode=True 时生效")


if __name__ == "__main__":
    asyncio.run(batch_scan(input_path=INPUT_FILE, output_path=OUTPUT_FILE, concurrency=20, test_mode=True, limit=0))
    # 只测前 5 条:   limit=5
    # 测全部:        limit=0