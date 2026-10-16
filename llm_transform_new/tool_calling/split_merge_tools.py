"""
输入拆分 + 输出合并工具。LLM 调用拆分工具解析表达式结构，
每处理完一个片段调用合并工具记录结果，全部完成后自动合并返回。

使用流程:
  1. LLM 调用 split_license_expression → 获取 session_id 和片段列表
  2. LLM 逐片段处理（识别/校验每个许可证标识符）
  3. 每完成一个片段，调用 merge_license_result 记录
  4. 最后一个片段完成时，自动合并返回最终表达式
"""

import uuid
import re
from langchain_core.tools import tool

# 会话存储: session_id → {structure, segments, results, count}
_sessions: dict[str, dict] = {}

# 分隔符匹配
_SEP_RE = re.compile(r'\s+and\s+|\s+or\s+|\s*&\s*|\s*\|\s*', re.IGNORECASE)
_PAREN_RE = re.compile(r'[()]')

# 拆分/合并工具用的 LLM provider（由 llm_scan 在运行时设置，默认 deepseek）
_SPLIT_PROVIDER = "deepseek"
_SPLIT_MODEL = None   # 拆分工具用的模型；None = 用 .env 里对应 provider 的默认模型


def set_split_provider(provider: str | None) -> None:
    """设置拆分工具使用的 LLM provider（deepseek/local/qwen/teco）。"""
    global _SPLIT_PROVIDER
    if provider:
        _SPLIT_PROVIDER = provider


def set_split_model(model: str | None) -> None:
    """设置拆分工具使用的模型（与主转换保持一致）；None = 用 .env 默认。"""
    global _SPLIT_MODEL
    _SPLIT_MODEL = model


def _split_model_name() -> str:
    """返回拆分工具当前实际使用的模型名（用于日志）。"""
    import os
    if _SPLIT_MODEL:
        return _SPLIT_MODEL
    if _SPLIT_PROVIDER == "local":
        return os.getenv("LLM_LOCAL_MODEL", "local")
    if _SPLIT_PROVIDER == "qwen":
        return os.getenv("LLM_QWEN_MODEL", "qwen")
    if _SPLIT_PROVIDER == "teco":
        return os.getenv("LLM_TECO_MODEL", "teco")
    return os.getenv("LLM_DEEPSEEK_MODEL", "deepseek-v4-pro")


def _response_content(response) -> str:
    """从 LLM 响应里取正文内容；content 为空时回退到 reasoning_content（Qwen 思考模型的思考内容）。"""
    content = getattr(response, "content", "")
    if content:
        if isinstance(content, str):
            return content.strip()
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        text = "".join(parts).strip()
        if text:
            return text
    rk = getattr(response, "reasoning_content", None)
    if not rk:
        rk = (getattr(response, "additional_kwargs", {}) or {}).get("reasoning_content", "")
    return str(rk or "").strip()


@tool
async def split_license_expression(expression: str) -> str:
    """
    拆分许可证表达式为独立片段，保留结构关系。

    使用时机：LLM 收到复合许可证输入时（含 AND/OR/&/| 等），必须先调用本工具拆分。
    本工具会返回一个 session_id 和所有需要处理的片段列表。

    参数:
    - expression: 完整的许可证表达式，如 "MIT AND Apache-2.0 OR (BSD-3 AND GPL-2)"

    返回:
    - JSON: {"session_id": "...", "segments": [...], "structure": "...", "expected_count": N}
    """
    import json

    # 预处理：换行替换为 AND，统一分隔符
    expression = re.sub(r'\n', ' AND ', expression)

    # 按操作符和括号拆分，保留结构
    tokens = re.split(r'(\s+and\s+|\s+or\s+|[&|()])', expression, flags=re.IGNORECASE)
    segments = []
    structure_parts = []

    for token in tokens:
        if not token.strip():
            continue
        if _SEP_RE.fullmatch(token):  # 先匹配原始 token（含空格）
            op = "&" if re.match(r'\s+and\s+|&', token, re.IGNORECASE) else "|"
            structure_parts.append(op)
        elif token == "(":
            structure_parts.append("(")
        elif token == ")":
            structure_parts.append(")")
        else:
            segments.append(token.strip())
            structure_parts.append("{" + str(len(segments) - 1) + "}")

    if not segments:
        return json.dumps({"error": "无法拆分表达式"}, ensure_ascii=False)

    regex_count = len(segments)
    structure = "".join(structure_parts).strip()

    # 用 LLM 验证 expected_count
    llm_count = await _verify_count_with_llm(expression, regex_count)

    if llm_count == regex_count:
        # 一致 → 直接通过正则结果
        expected_count = regex_count
    else:
        # 不一致 → LLM 做完整结构分析
        llm_result = await _full_split_with_llm(expression)
        if llm_result:
            # LLM 分析成功 → 用 LLM 结果替换
            segments = llm_result["segments"]
            structure = llm_result["structure"]
            expected_count = llm_result["expected_count"]
        else:
            # LLM 分析失败 → 回退到正则结果
            expected_count = regex_count

    session_id = uuid.uuid4().hex[:12]

    _sessions[session_id] = {
        "segments": segments,
        "structure": structure,
        "results": {},
        "expected_count": expected_count,
    }

    return json.dumps({
        "session_id": session_id,
        "segments": segments,
        "structure": structure,
        "expected_count": expected_count,
    }, ensure_ascii=False, indent=2)


def _get_llm_for_split(thinking: bool = False) -> object:
    """获取 LLM 实例（provider 由 set_split_provider 设置，默认 deepseek）。thinking=True 开启思考模式（仅 deepseek）。

    复用 call_llm_langchain._get_llm，以便正确应用各 provider 的差异配置：
    - local：关闭 SSL 验证、跳过代理、支持 Basic 认证
    - deepseek：额外传 thinking 参数
    """
    from call_llm_langchain import _get_llm
    extra = {}
    if _SPLIT_PROVIDER == "deepseek":
        extra = {"thinking": {"type": "enabled" if thinking else "disabled"}}
    return _get_llm(
        provider=_SPLIT_PROVIDER,
        model=_SPLIT_MODEL,  # 与主转换保持一致；None = 用 .env 里对应 provider 的默认模型
        extra_body=extra if extra else None,
    )


def _dedup_expression(expr: str) -> str:
    """按层级去重：只去除同一括号层级的重复许可证片段。

    规则：
    - 同一层级中重复的许可证 → 去重
    - 括号内和括号外 → 不同层级，不去重
    - 不同括号组之间 → 不同层级，不去重
    - 例外：两个括号组内容完全相同时 → 整组去重
    例如:
      MIT&MIT → MIT（去重）
      MIT&(MIT|GPLv2 or later) → 不去重（括号内外的 MIT 在不同层级）
      (MIT|MIT) → (MIT)（括号内同级去重）
      (A|B)&(A|B) → (A|B)（括号组内容完全相同 → 整组去重）
      (A|B)&(A|C) → 不去重（括号组内容不同）
    """

    def _clean_separators(s: str) -> str:
        """清理多余的操作符。"""
        s = re.sub(r"[&|]{2,}", "&", s)      # 连续操作符合并
        s = re.sub(r"[&|]\)", ")", s)        # 清理 ) 前的多余操作符
        s = re.sub(r"\([&|]", "(", s)        # 清理 ( 后的多余操作符
        s = re.sub(r"^[&|]|[&|]$", "", s)    # 首尾操作符
        s = re.sub(r"\(\s*\)", "", s)         # 空括号
        return s

    def _tokenize(s: str) -> list[str]:
        tokens = []
        i = 0
        while i < len(s):
            if s[i] in "&|()":
                tokens.append(s[i])
                i += 1
            else:
                j = i
                while j < len(s) and s[j] not in "&|()":
                    j += 1
                token = s[i:j].strip()
                if token:
                    tokens.append(token)
                i = j
        return tokens

    def _dedup_tokens_with_parens(tokens):
        """递归处理括号：先递归处理内层，再对当前层去重。"""
        result = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t == "(":
                depth = 1
                j = i + 1
                while j < len(tokens) and depth > 0:
                    if tokens[j] == "(":
                        depth += 1
                    elif tokens[j] == ")":
                        depth -= 1
                    j += 1
                inner = _dedup_tokens_with_parens(tokens[i+1:j-1])
                result.append("(")
                result.extend(inner)
                result.append(")")
                i = j
            elif t in ("&", "|"):
                result.append(t)
                i += 1
            else:
                result.append(t)
                i += 1
        return _dedup_list(result)

    def _dedup_list(tokens):
        """对当前层级的 tokens 去重。
        - 遇到 (...) 时，将整个括号组作为一个整体：内容相同则去重，内容不同则保留。
        - 括号内部已在 _dedup_tokens_with_parens 递归中独立去重。
        """
        seen = set()
        result = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t == "(":
                # 找到匹配的 )，将整个括号组作为整体比较
                depth = 1
                j = i + 1
                while j < len(tokens) and depth > 0:
                    if tokens[j] == "(":
                        depth += 1
                    elif tokens[j] == ")":
                        depth -= 1
                    j += 1
                group_sig = "".join(tokens[i:j])  # 如 "(MIT|GPLv2 or later)"
                if group_sig in seen:
                    i = j  # 完全相同的括号组 → 去重
                else:
                    seen.add(group_sig)
                    result.extend(tokens[i:j])
                    i = j
            elif t in ("&", "|", ")"):
                result.append(t)
                i += 1
            elif t == "none" and "none" in seen:
                i += 1
            elif t in seen:
                i += 1
            else:
                seen.add(t)
                result.append(t)
                i += 1
        return result

    if not expr:
        return expr
    tokens = _tokenize(expr)
    deduped = _dedup_tokens_with_parens(tokens)
    result = "".join(deduped)
    return _clean_separators(result)

# 版本修饰语正则："v2 or later", "v3 or newer", "or any later version"
_VERSION_OR_RE = re.compile(r'\bv?\d[\d.]*\s+or\s+(later|newer|any\s+later\s+version)\b', re.IGNORECASE)

async def _verify_count_with_llm(expression: str, regex_count: int) -> int:
    """用 LLM 快速验证正则拆分出的片段数量是否正确（思考关闭）。"""
    # 本地预处理：含版本修饰语的 or 不应拆分
    if _VERSION_OR_RE.search(expression) and regex_count > 1:
        return 1  # 直接修正：版本 or later 是一个整体

    try:
        from langchain_core.messages import HumanMessage
        llm = _get_llm_for_split(thinking=False)
        prompt = (
            f"输入: \"{expression}\"\n"
            f"判断应拆分成几个独立的许可证标识符:\n"
            f"- and/AND → 拆分\n"
            f"- or/OR → 拆分\n"
            f"- 注意：换行符也当做 and 拆分\n"
            f"- 有的情况两个独立的许可证标识符只用一个空格分开，需要仔细辨别\n"
            f"- 但 \"v2 or later\" \"v3 or newer\" \"or any later version\" 是版本号修饰 → 不拆\n"
            f"- \"WITH xxx-exception\" → 不拆\n"
            f"- 同一许可证不重复记录原则（按层级）：仅同一括号层级内的重复才算重复（如 MIT&MIT 记作1），\n"
            f"  不同层级（括号内外、不同括号组内容不同）不算重复；\n"
            f"  但若两个括号组内容完全相同时（如 (A|B)&(A|B)），也视为重复并去重，数量记作1\n"
            f"只输出一个整数。"
        )
        print(f"  [LLM-fast] 调用 {_split_model_name()} 验证 count...")
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        count = int(_response_content(response))
        if 1 <= count <= 20:
            return count
    except Exception as e:
        print(f"  [WARN] LLM 计数验证失败: {e}")
    return regex_count


async def _full_split_with_llm(expression: str, max_retries: int = 3) -> dict | None:
    """
    当 LLM 的 expected_count 与正则结果不一致时，让 LLM 做完整结构分析（思考开启）。
    返回 {"segments": [...], "structure": "...", "expected_count": N} 或 None。
    """
    import json
    from langchain_core.messages import HumanMessage, SystemMessage
    llm = _get_llm_for_split(thinking=True)

    system_prompt = """你是一个许可证表达式解析器。请分析用户的输入，提取其中所有独立的许可证标识符和它们之间的逻辑关系。

必须严格识别以下内容：
1. 逻辑运算符：and (AND) 和 or (OR) 表示多个许可证的组合，用 & 代表 AND，用 | 代表 OR
2. 括号：保留原始括号结构以表示优先级
3. 独立标识符：每个被 and/or/括号分隔的部分都是一个独立的许可证标识符
4. 同一许可证不重复输出原则（按层级去重）：仅去除同一括号层级内重复的许可证。不同层级之间（如括号内与括号外、不同括号组内容不同）即使许可证相同也不去重，各自独立输出。例外：若两个括号组的内容完全相同时（如 (A|B)&(A|B)），也视为重复，只输出一个括号组。
	   例如：MIT&MIT → 1个（同级去重）；MIT&(MIT|GPL) → 2个MIT分别在括号内外，不去重;(A|B)&(A|B) → 1个（整组去重）
5. 重要例外：
   - "v2 or later" / "v3 or newer" / "or any later version" 是版本修饰语，整体算一个许可证，不拆分
   - "LGPL v2 or later" → 1 个许可证，不是 2 个
   - "WITH xxx-exception" 整体算一个许可证，不拆分
   - "GPLv2 or later" → 1 个，不是 2 个

输出格式严格按以下 JSON 结构，不要任何解释文字：
{"segments": ["标识符1", "标识符2", ...], "structure": "{0}&{1}|({2}&{3})", "expected_count": 4}

示例：
输入: "LGPL v2 or later"
输出: {"segments":["LGPL v2 or later"],"structure":"{0}","expected_count":1}

输入: "MIT AND Apache-2.0 OR (BSD-3 AND GPL-2)"
输出: {"segments":["MIT","Apache-2.0","BSD-3","GPL-2"],"structure":"{0}&{1}|({2}&{3})","expected_count":4}

输入: "GPL-2.0-only WITH Classpath-exception-2.0"
输出: {"segments":["GPL-2.0-only WITH Classpath-exception-2.0"],"structure":"{0}","expected_count":1}"""

    print(f"  [LLM-full] 调用 {_split_model_name()} 完整结构分析...")
    for attempt in range(max_retries):
        try:
            response = await llm.ainvoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=expression),
            ])
            content = _response_content(response)
            # 去掉可能的 markdown 包裹
            content = re.sub(r'^```(?:json)?\s*\n?', '', content)
            content = re.sub(r'\n?```\s*$', '', content)

            result = json.loads(content)

            # 结构校验
            if not isinstance(result, dict):
                continue
            segs = result.get("segments")
            struct = result.get("structure")
            cnt = result.get("expected_count")
            if not isinstance(segs, list) or not segs:
                continue
            if not isinstance(struct, str):
                continue
            if not isinstance(cnt, int) or cnt != len(segs):
                continue

            return {"segments": segs, "structure": struct, "expected_count": cnt}

        except Exception:
            if attempt < max_retries - 1:
                continue

    return None  # 全部重试失败


@tool
def merge_license_result(input_json: str) -> str:
    """
    记录一个片段的结果，当全部片段完成后自动合并为最终表达式。

    使用时机：LLM 每完成一个许可证片段的识别后，调用本工具记录结果。
    当所有片段都记录完成后，本工具自动按原始结构合并并返回最终结果。

    参数 input_json 格式:
    {"session_id": "...", "segment_index": 0, "result": "MIT"}

    返回:
    - 若未完成: {"status": "pending", "completed": N, "expected": M}
    - 若已完成: {"status": "done", "expression": "最终合并结果"}
    """
    import json

    try:
        params = json.loads(input_json)
        session_id = params["session_id"]
        segment_index = params["segment_index"]
        result = params["result"]
    except (json.JSONDecodeError, KeyError) as e:
        return json.dumps({"error": f"参数解析失败: {e}"}, ensure_ascii=False)

    session = _sessions.get(session_id)
    if not session:
        return json.dumps({"error": "无效的 session_id"}, ensure_ascii=False)

    # 记录结果
    session["results"][str(segment_index)] = result
    completed = len(session["results"])

    if completed < session["expected_count"]:
        return json.dumps({
            "status": "pending",
            "completed": completed,
            "expected": session["expected_count"],
        }, ensure_ascii=False)

    # 全部完成：按结构合并
    expr = session["structure"]
    for i in range(session["expected_count"]):
        placeholder = "{" + str(i) + "}"
        val = session["results"].get(str(i), "none")
        expr = expr.replace(placeholder, val)

    # 去重：相同许可证片段只保留第一个
    expr = _dedup_expression(expr)

    # 清理会话
    del _sessions[session_id]

    return json.dumps({
        "status": "done",
        "expression": expr,
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import asyncio, json

    async def _test():
        tests = [
"MIT&MIT&MIT&MIT" ,
"LGPL-2.1-or-later&MIT&LGPL-2.1-or-later",
"MIT&(MIT|GPL)"
             ]
        for expr in tests:
            print(f"\n输入: {expr}")
            r = await split_license_expression.ainvoke(expr)
            data = json.loads(r)
            print(f"  segments: {data.get('segments', [])}")
            print(f"  structure: {data.get('structure', '')}")
            print(f"  expected_count: {data.get('expected_count', 0)}")

            # Test merge
            sid = data.get("session_id", "")
            for i in range(data.get("expected_count", 0)):
                result = f"RESULT-{i}"
                inp = json.dumps({"session_id": sid, "segment_index": i, "result": result})
                out = json.loads(merge_license_result.invoke(inp))
                if out.get("status") == "done":
                    print(f"  合并结果: {out.get('expression', '')}")

    asyncio.run(_test())

