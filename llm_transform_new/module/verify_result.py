"""
验证 llm_scan.py 输出结果的可信度。
用 LLM 判断每个非标许可证文本的转换结果是否正确。
"""

import asyncio
import json
import time
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from call_llm_langchain import _get_llm
from data_tool import load_jsonl, load_done_ids, record_id

# ========== 可修改配置 ==========
VERIFY_PROVIDER = "deepseek"             # deepseek=DeepSeek, local=本地Qwen,qwen=Qwen
VERIFY_MODEL = "deepseek-v4-flash"       # 验证用模型: deepseek-v4-pro / qwen3.7-plus / qwen3.6-27b
THINKING_ENABLED = True             # 思考模式 (Qwen等非DeepSeek模型可能不支持)
CONCURRENCY = 15                       # 并发数（DeepSeek 账户并发上限 5，超出会 429）
# ===============================


VERIFY_SYSTEM_PROMPT = """你是一个开源许可证合规专家。你需要判断非标许可证文本到标准化 SPDX 标识符的转换是否正确。

## 转换系统的核心规则（这些规则决定了转换结果如何生成）

### 1. 最优先级规则
如果在 SPDX 数据库中有完全一致的标识符，直接输出，不做任何修改。
例如: "MIT" → "MIT", "Apache-2.0" → "Apache-2.0", "GPL-3.0-only" → "GPL-3.0-only"

### 2. WITH 规则
输入中含 WITH → 输出必须含 ` WITH `（两边空格）。拆分左右两边分别校验：
- 左边：SPDX 主体或 LicenseRef-cleansource- 前缀标识符
- 右边：SPDX exception 标识符
- 输出格式: `左边 WITH 右边`，严禁用 -with- 或 & 替代
例如: "GPL-2.0-only WITH Classpath-exception-2.0" → 正确保留 WITH

### 3. ScanCode 规则
SPDX 中没有，但 ScanCode 中有完全一致标识符 → 加前缀 `LicenseRef-cleansource-` 输出。
- 原文含 LicenseRef-scancode- 前缀 → 替换为 LicenseRef-cleansource-
- 原文是 scancode 私有许可证 → 加 LicenseRef-cleansource- 前缀
例如: "3Com Microcode" → "LicenseRef-cleansource-3com-microcode"

### 4. 专有许可证
公司专有许可证（SPDX和ScanCode都没有收录的陌生许可证） 需要你以一个专业的许可证律师的视角判断该许可证是否是专有许可证，若是→ `LicenseRef-cleansource-proprietary-license`

### 5. Commercial 规则
只要含 Commercial 字段（非 Non-commercial）→ `Commercial`

### 6. 未知版本规则
只有许可证名称无版本号 → 按映射表查默认版本：
  AFL→AFL-3.0, AGPL→AGPL-3.0-only, Apache→Apache-2.0, Artistic→Artistic-2.0,
  BSD→BSD-3-Clause, CC-BY→CC-BY-4.0, CC-BY-NC→CC-BY-NC-4.0, CC-BY-ND→CC-BY-ND-4.0,
  CC-BY-SA→CC-BY-SA-4.0, CDDL→CDDL-1.1, ECL→ECL-2.0, EPL→EPL-2.0, EUPL→EUPL-1.2,
  GPL→GPL-3.0-only, LGPL→LGPL-3.0-only, MPL→MPL-2.0, OFL→OFL-1.1, OSL→OSL-3.0,
  PHP→PHP-3.01, Python→Python-2.0.1
不在映射表 → 自行判断 → 无法判断 → `LicenseRef-cleansource-unknown`

### 7. 组合逻辑规则
原文 and → &，原文 or → |，&|两边无空格
例如: "MIT and Apache-2.0" → "MIT&Apache-2.0"

### 8. GPL/LGPL/AGPL 版本规则
"-only" 仅当原文明确排除后续版本时使用。默认使用 "-or-later"。
- "GPLv2" → "GPL-2.0-only"（明确 v2）
- "GPLv2 or later" → "GPL-2.0-or-later"
- "GPL"（无版本）→ "GPL-3.0-only"（按映射表）

### 9. 输出格式总览
输出只能是以下形式之一：
- SPDX 标准标识符 (如 MIT, GPL-3.0-only, Apache-2.0)
- SPDX 标识符组合 (& 或 | 连接, 两边无空格)
- LicenseRef-cleansource- 前缀标识符
- 带 ` WITH ` 的标识符 (WITH 两边空格)
- Commercial
- LicenseRef-cleansource-proprietary-license
- LicenseRef-cleansource-unknown
- none (非许可证内容)

### 10. 特殊判断规则
- & 连接 → 原文是 AND/and 关系 → 多个许可证同时适用 → 正确
- | 连接 → 原文是 OR/or 关系 → 多选一 → 正确
- LicenseRef-cleansource- 前缀 → 原文描述的确实是非 SPDX 标准许可证 → 正确
- none → 原文确实不是许可证内容 → 正确
- 原文含多个许可证用逗号/换行分隔 → 正确做法是用 & 连接各个标识符
- WITH 两边都不允许输出 unknown

### 11. 输入含模糊 exception 描述（WITH exceptions 规则）
当输入中以 "XXX with exceptions" 或 "XXX with exception" 的模糊形式出现时（如 "GPLv3 with exceptions"），
系统会针对左边许可证（如 GPLv3、LGPLv2）推断其最常见的 exception 配对，输出完整的 WITH 表达式。

判断标准：
- "GPLv3 with exceptions" → "GPL-3.0-only WITH 某个具体exception" 是正确的
- "GPLv2 with exceptions" → "GPL-2.0-only WITH 某个具体exception" 是正确的
- 多个 "X with exceptions" 用 or 组合 → 各自独立推断 exception，用 | 连接输出

常见许可证的默认 exception 配对：
- GPL-3.0-only: 389-exception、Classpath-exception-2.0、GCC-exception-3.1、Autoconf-exception-3.0、GPL-3.0-linking-exception、Font-exception-2.0
- GPL-2.0-only: 389-exception、Classpath-exception-2.0、GCC-exception-2.0、Autoconf-exception-2.0、freertos-exception-2.0、Bootloader-exception、Linux-syscall-note、Font-exception-2.0
- LGPL-2.0-only/LGPL-3.0-only: Classpath-exception-2.0、FLTK-exception、GPL-3.0-linking-exception、LGPL-3.0-linking-exception
- Apache-2.0: LLVM-exception、Swift-exception
- 若不是常见配对但逻辑合理，仍判定为 correct，confidence=medium

验证示例（均为 correct）：
- "GPLv3 with exceptions or GPLv2 with exceptions" → "GPL-3.0-only WITH xxx|GPL-2.0-only WITH xxx" ✅
- "GPLv3 with exceptions or LGPLv2 with exceptions" → "GPL-3.0-only WITH xxx|LGPL-2.0-only WITH xxx" ✅
- "GPLv3 with exceptions" → "GPL-3.0-only WITH xxx"（xxx 为 SPDX exception 列表中的任一项）✅


### 12. 同时出现同家族已标明版本和未标明版本的规则
当输入中同一许可证家族同时出现已标明版本（如 GPL-2.0+、Apache-2.0）和未标明版本（如 GNU Public License、Apache License）时：
- 以已标明版本的为准，未标明版本的忽略，不重复输出
- 只有当输入中仅存在未标明版本时，才触发规则6（未知版本规则）
- 此规则适用于同一括号层级内的去重；若两者在不同层级（如括号内外），则各层独立保留，不去重

示例：
- `GPL-2.0+\nGNU Public License (GPL)` → `GPL-2.0-or-later`（已标明的 GPL-2.0+ 为准，忽略未标明的 GPL）
- `Apache-2.0 and Apache License` → `Apache-2.0`（已标明的 Apache-2.0 为准）
- `MIT and (Apache License and Apache-2.0)` → `MIT&(Apache License&Apache-2.0)`（不同层级，各层独立，不去重；括号内由规则9按层级去重）
- 仅输入 `GPL` 无其他版本 → 触发规则6 → `GPL-3.0-only`


## 评分标准
1. 转换完全正确 → True, confidence=high
2. 基本正确但小瑕疵（版本号微调，但不影响语义）→ True, confidence=medium
3. 可能正确但无法确定（license名称模糊）→ True, confidence=low
4. 明显错误（标识符不存在、关系错误、版本号严重不对）→ False, confidence=high
5. 不太可能正确 → False, confidence=medium
6. 可疑但可能是对的 → False, confidence=low

输出严格 JSON，不含任何其他内容：
{"correct": true/false, "confidence": "high"/"medium"/"low"}
"""


def _get_license_text(item: dict) -> str:
    """从记录中取许可证原文：兼容 license / license_text / 原许可证 / 原始许可证。"""
    for field in ("license_text", "license", "原许可证", "原始许可证"):
        val = item.get(field)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


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


async def verify_one(license_text: str, license_spdx_name: str, model: str | None = None,
                     provider: str | None = None, show_reasoning: bool = False) -> dict:
    """验证单条转换结果的正确性。show_reasoning=True 时打印思考过程。"""
    messages = [
        SystemMessage(content=VERIFY_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"原文: \"{license_text}\"\n"
            f"转换结果: \"{license_spdx_name}\"\n\n"
            f"请判断这个转换是否正确。"
        )),
    ]
    content = ""  # 提前初始化，避免异常分支里 content 未定义

    try:
        # thinking 参数仅 DeepSeek 支持，Qwen 不传
        used_provider = provider or VERIFY_PROVIDER
        extra = {}
        if used_provider == "deepseek":
            extra["thinking"] = {"type": "enabled" if THINKING_ENABLED else "disabled"}

        llm = _get_llm(
            provider=used_provider,
            model=model or VERIFY_MODEL,
            temperature=0,
            max_tokens=4096,  # Qwen推理模型需要更多token, 256不够
            extra_body=extra if extra else None,
        )

        response = await _ainvoke_with_retry(llm, messages)

        # 显示思考过程
        reasoning = getattr(response, "reasoning", "") or ""
        if show_reasoning and reasoning:
            print(f"  [思考] {str(reasoning)[:800]}")
            if len(str(reasoning)) > 800:
                print(f"  ...（思考共 {len(str(reasoning))} 字符）")

        content = response.content.strip() if response.content else ""

        # 去掉 markdown 包裹
        import re as _re
        content = _re.sub(r'^```(?:json)?\s*\n?', '', content)
        content = _re.sub(r'\n?```\s*$', '', content)

        result = json.loads(content)
        return {
            "correct": result.get("correct", False),
            "confidence": result.get("confidence", "medium"),
            "reasoning": str(reasoning)[:500] if show_reasoning else "",
        }
    except _RETRYABLE_EXCEPTIONS:
        # 瞬态错误重试后仍失败：向上抛出，由 verify_batch 跳过不落盘（下次续跑重试），
        # 避免错误地把「审核失败」当成「审核不通过(correct=false)」写进结果。
        raise
    except Exception as e:
        print(f"  [VERIFY ERROR] {e} | raw_content={repr(content[:200])}")
        return {"correct": False, "confidence": "low", "reasoning": ""}


async def verify_batch(
    input_path: str,
    output_path: str | None = None,
    model: str | None = None,
    concurrency: int = CONCURRENCY,
    show_reasoning: bool = False,  # 打印 LLM 思考过程
    provider: str | None = None,   # deepseek / local
):
    """
    批量验证 test_output.json 中的转换结果。

    参数:
    - input_path: test_output.json 文件路径
    - output_path: 验证结果输出文件的完整路径（默认：与输入同目录，命名 <原始数据名称>_verified.json）
    - model: 验证用模型
    - concurrency: 并发数
    - show_reasoning: 是否打印 LLM 思考过程
    - provider: deepseek=DeepSeek, local=本地Qwen
    """
    input_path = Path(input_path)
    if not input_path.exists():
        print(f"文件不存在: {input_path}")
        return

    # 读输入：兼容 JSON 数组 和 JSONL
    raw = input_path.read_text(encoding="utf-8").strip()
    if raw.startswith("["):
        data = json.loads(raw)
    else:
        data = load_jsonl(input_path)
    total = len(data)
    if total == 0:
        print("数据为空")
        return

    if output_path is None:
        # 默认：与输入同目录，命名 <原始数据名称>_verified.json（原始数据名称 = 去掉 "_output" 后缀的输入名）
        stem = input_path.stem
        if stem.endswith("_output"):
            stem = stem[: -len("_output")]
        output_path = input_path.parent / f"{stem}_verified.json"
    else:
        output_path = Path(output_path)

    used_model = model or VERIFY_MODEL
    used_provider = provider or VERIFY_PROVIDER
    print(f"验证: {used_provider}/{used_model} | 思考: {'ON' if THINKING_ENABLED else 'OFF'}")
    print(f"输入: {input_path} ({total} 条)")
    print(f"输出: {output_path}")
    print("-" * 60)

    # 断点续跑：record_id（优先用输入里已有的，否则现算）+ 已完成集合
    rids = [r.get("record_id") or record_id(r) for r in data]
    done = load_done_ids(output_path)
    todo = [i for i in range(total) if rids[i] not in done]
    todo_count = len(todo)
    if todo_count == 0:
        print("全部已处理，无需重跑。")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)

    t_start = time.perf_counter()
    sem = asyncio.Semaphore(concurrency)
    correct_count = 0
    completed = 0
    failed = 0

    async def process_one(i: int):
        item = data[i]
        async with sem:
            try:
                verdict = await verify_one(
                    _get_license_text(item), item.get("license_spdx_name", ""),
                    model=used_model, provider=used_provider,
                    show_reasoning=show_reasoning,
                )
            except Exception:
                # 审核失败（重试后仍失败）：返回 None 标记，不落盘，下次续跑重试
                print(f"  [{i+1}/{total}] 审核失败(跳过，下次续跑重试): {_get_license_text(item)[:50]!r}")
                return None
        rec = dict(item)          # 浅拷贝，保留所有原始字段
        rec["record_id"] = rids[i]
        rec["correct"] = verdict["correct"]
        rec["confidence"] = verdict["confidence"]
        status = "✅" if verdict["correct"] else "❌"
        print(f"  [{i+1}/{total}] {status} ({verdict['confidence']:6s}) {_get_license_text(item)[:60]}")
        return rec

    tasks = [process_one(i) for i in todo]

    # 逐条落盘（append + line buffered，中断可续跑）
    with output_path.open("a", encoding="utf-8", buffering=1) as out:
        for coro in asyncio.as_completed(tasks):
            rec = await coro
            if rec is None:
                # 审核失败记录不落盘，避免被断点续跑误判为已完成
                failed += 1
                continue
            if rec.get("correct"):
                correct_count += 1
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            completed += 1

    total_time = time.perf_counter() - t_start

    print("-" * 60)
    print(f"本次验证: {completed}/{todo_count} 条（其中正确 {correct_count}） | 失败 {failed} 条 | {total_time:.1f}s")
    print(f"结果: {output_path}")

    return load_jsonl(output_path)


def verify_file(input_path: str, output_path: str | None = None, model: str | None = None,
                provider: str | None = None):
    """同步入口：验证单个文件。"""
    return asyncio.run(verify_batch(input_path, output_path, model=model, provider=provider))


if __name__ == "__main__":
    import sys

    target=Path(__file__).parent /"数据"/"0827非标转化"/"license_need_check_20260825_output.json"
    output_path=Path(__file__).parent /"数据"/"0827非标转化"/"license_need_check_20260825_verified.json"
    if target and output_path:
        # 指定路径：python verify_result.py <test_output.json路径>
        if not target.exists():
            print(f"文件不存在: {target}")
            sys.exit(1)
        asyncio.run(verify_batch(str(target), str(output_path)))
    else:
        # 默认：找最新的测试文件夹
        test_dir = Path(__file__).parent / "数据" / "模型测试"
        test_folders = sorted([d for d in test_dir.iterdir() if d.is_dir()], reverse=True)
        if test_folders:
            latest = test_folders[0]
            test_output = latest / "test_output.json"
            if test_output.exists():
                asyncio.run(verify_batch(str(test_output)))
            else:
                print(f"未找到 test_output.json: {latest}")
        else:
            print("没有测试文件夹")
