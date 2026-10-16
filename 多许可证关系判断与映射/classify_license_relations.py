#!/usr/bin/env python3
"""Classify relationships among licenses in a large Maven JSON dataset.

单许可证记录直接判定为 SINGLE 写入结果；多许可证记录（license_text 含 <> 标签或长度超过阈值）才调用 LLM 判断。
Uses an OpenAI-compatible /chat/completions endpoint and writes one JSON object
per line, allowing safe resume after interruption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator


# ──── 全局配置（按需修改） ────
BASE_DIR = Path(__file__).resolve().parent

# 输入文件目录 / 文件
INPUT_DIR = BASE_DIR / "数据" / "0828maven爬取+多许可证判断"
INPUT_FILE = INPUT_DIR / "0828_test.json"

# 输出文件目录 / 文件
OUTPUT_DIR = INPUT_DIR
OUTPUT_FILE = OUTPUT_DIR / "0828_test_output.jsonl"

# 模型与 API
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-f856d72fd35d460a86bc5b80dd2fd90d")
DEEPSEEK_MODEL = "deepseek-v4-flash"


RELATIONS = {"AND", "OR", "NO_EXPLICIT_RELATION", "SINGLE", "UNKNOWN"}

SYSTEM_PROMPT = """你是开源许可证元数据审计员。你要识别“组件主体许可证”及其逻辑关系，并输出 JSON。

信息源的含义和优先级（必须严格执行）：
1. pom_header_context 是 POM 文件开头。这里的版权/许可证注释、SPDX-License-Identifier 或明确的许可声明代表整个组件，优先级最高。
2. all_declared_licenses 是许可证字段列出的所有许可证，可能同时包含组件主体、示例、第三方内容、内嵌/重打包代码等许可证，不能默认全部视为组件主体许可证。
3. 如果 pom_header_context 明确声明了组件许可证，只以该声明确定 effective_license_names 和关系；all_declared_licenses 只用于名称核对，不得把其中其他许可证加入主体关系。
4. 只有 pom_header_context 没有任何明确许可证声明时，才使用 all_declared_licenses 判断，并将 license_basis 设为 all_declared_licenses_fallback。
5. SPDX-License-Identifier 是高度明确的关系表达式。例如“EPL-2.0 OR GPL-2.0 WITH Classpath-exception-2.0”必须原样理解为 OR；WITH 是许可证例外组合的一部分，不是 AND。

关系判定：
- OR：原文明示任选其一，如 or、either...or、dual licensed、Secondary Licenses、SPDX 中的 OR。
- AND：原文明示多个主体许可证共同适用。不同目录、示例或第三方内容分别采用不同许可证，不应简单判为主体 AND。
- NO_EXPLICIT_RELATION：采用回退许可证字段且存在多个许可证，但没有明确 AND/OR。并列多个 license 节点本身不是关系证据。
- SINGLE：最终组件主体许可证只有一个。
- UNKNOWN：材料异常、冲突或无法判断。

effective_license_names 只列组件主体许可证。excluded_license_names 列出许可证字段中未纳入主体关系的许可证，并简述排除原因。
evidence_quote 必须逐字复制输入中的最短充分原文，不得改写；优先使用 SPDX 行，否则使用明确许可句。没有证据时为空字符串。
confidence 是 0 到 1。证据不明确、来源冲突、UNKNOWN 或 confidence < 0.80 时 needs_human_review=true。
只返回一个 JSON 对象，不要 Markdown。JSON 示例：
{"license_basis":"pom_header","effective_license_names":["EPL-2.0","GPL-2.0 WITH Classpath-exception-2.0"],"normalized_license_names":["EPL-2.0","GPL-2.0 WITH Classpath-exception-2.0"],"excluded_license_names":["MIT"],"exclusion_reason":"MIT 仅适用于内嵌第三方内容","license_relation":"OR","relation_expression":"(EPL-2.0 OR GPL-2.0 WITH Classpath-exception-2.0)","evidence_quote":"SPDX-License-Identifier: EPL-2.0 OR GPL-2.0 WITH Classpath-exception-2.0","evidence_location":"pom_header","reasoning_summary":"POM 开头 SPDX 明确声明组件主体为二选一许可","confidence":0.99,"needs_human_review":false,"human_review_reasons":[]}"""


def stream_json_array(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[dict[str, Any]]:
    """Incrementally parse a top-level JSON array without loading it all."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8-sig") as fh:
        buf, pos, started, eof = "", 0, False, False
        while True:
            if pos > chunk_size:
                buf, pos = buf[pos:], 0
            if not eof and (len(buf) - pos < chunk_size // 2):
                chunk = fh.read(chunk_size)
                if chunk:
                    buf += chunk
                else:
                    eof = True
            while pos < len(buf) and buf[pos].isspace():
                pos += 1
            if not started:
                if pos >= len(buf):
                    if eof:
                        raise ValueError("空 JSON 文件")
                    continue
                if buf[pos] != "[":
                    raise ValueError("输入必须是顶层 JSON 数组")
                pos += 1
                started = True
                continue
            while pos < len(buf) and (buf[pos].isspace() or buf[pos] == ","):
                pos += 1
            if pos < len(buf) and buf[pos] == "]":
                return
            try:
                value, end = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if eof:
                    raise
                chunk = fh.read(chunk_size)
                if chunk:
                    buf += chunk
                else:
                    eof = True
                continue
            if not isinstance(value, dict):
                raise ValueError("数组元素必须是 JSON 对象")
            yield value
            pos = end


def extract_license_names(text: str) -> list[str]:
    names = re.findall(r"<license\b[^>]*>.*?<name\b[^>]*>(.*?)</name>", text, re.I | re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip() for x in names]


def compact_pom(pom: str, max_chars: int) -> tuple[str, bool]:
    """Keep only the POM beginning, which is the authoritative component header."""
    return pom[:max_chars], len(pom) > max_chars


def compact_text(text: str, max_chars: int) -> tuple[str, bool]:
    return text[:max_chars], len(text) > max_chars


def record_id(item: dict[str, Any]) -> str:
    identity = f"{item.get('purl', '')}\0{item.get('version', '')}\0{item.get('source', '')}"
    return hashlib.sha256(identity.encode()).hexdigest()[:20]


def is_multi_license(item: dict[str, Any], max_single_chars: int) -> bool:
    """判断记录是否为多许可证：license_text 含 <> 标签，或长度超过阈值。

    返回 False 表示单许可证（短文本且无 <> 标签），可跳过 LLM 直接判定为 SINGLE。
    """
    text = str(item.get("license_text", "") or "").strip()
    if not text:
        return False
    if "<" in text or ">" in text:
        return True
    return len(text) > max_single_chars


def single_license_result(item: dict[str, Any], model: str) -> dict[str, Any]:
    """单许可证记录：直接判定为 SINGLE，不调用 LLM，写出与 normalize_result 同构的结果。"""
    rid = record_id(item)
    text = str(item.get("license_text", "") or "").strip()
    names = extract_license_names(text) or ([text] if text else [])
    return {
        "record_id": rid,
        "purl": item.get("purl"),
        "version": item.get("version"),
        "source": item.get("source"),
        "license_source": item.get("license_source"),
        "license_basis": "all_declared_licenses_fallback",
        "license_names": names,
        "effective_license_names": names,
        "all_declared_license_names": names,
        "normalized_license_names": names,
        "excluded_license_names": [],
        "exclusion_reason": "",
        "license_relation": "SINGLE",
        "relation_expression": names[0] if names else "",
        "evidence_quote": "",
        "evidence_location": "all_declared_licenses",
        "reasoning_summary": "单许可证，直接判定无需 LLM",
        "confidence": 1.0,
        "needs_human_review": False,
        "human_review_reasons": [],
        "processing_status": "success",
        "model": model,
        "prompt_version": "license-relation-v2-component-header-priority",
        "input_truncated": False,
        "usage": {},
    }


def call_model(api_url: str, api_key: str, model: str, payload: dict[str, Any],
               timeout: int, max_tokens: int) -> dict[str, Any]:
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(api_url, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        response_text = resp.read().decode("utf-8", errors="replace").strip()
    if not response_text:
        raise ValueError("DeepSeek HTTP 响应体为空")
    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"DeepSeek HTTP 响应不是 JSON: {response_text[:200]!r}") from exc
    try:
        choice = raw["choices"][0]
        content = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"DeepSeek 响应结构异常: {response_text[:500]}") from exc
    content = content.strip()
    if not content:
        finish_reason = choice.get("finish_reason", "unknown")
        raise ValueError(f"DeepSeek message.content 为空，finish_reason={finish_reason}")
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S).strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        # Tolerate a short explanation surrounding a JSON object.
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"模型内容不是有效 JSON: {content[:300]!r}") from exc
        result = json.loads(content[start:end + 1])
    result["_usage"] = raw.get("usage", {})
    return result


def classify_one(item: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    rid = record_id(item)
    original_license_text = str(item.get("license_text", ""))
    license_text, license_truncated = compact_text(original_license_text, args.max_license_chars)
    relevant_pom, truncated = compact_pom(str(item.get("pom_text", "")), args.max_pom_chars)
    payload = {
        "purl": item.get("purl"), "version": item.get("version"),
        "pom_header_context": relevant_pom,
        "all_declared_licenses": license_text,
        "required_output_fields": {
            "license_basis": "pom_header|all_declared_licenses_fallback|unknown",
            "effective_license_names": ["组件主体许可证名称"],
            "normalized_license_names": ["尽可能给出 SPDX 标识；不确定则保留原名"],
            "excluded_license_names": ["许可证字段中未纳入主体关系的名称"],
            "exclusion_reason": "排除原因摘要",
            "license_relation": "AND|OR|NO_EXPLICIT_RELATION|SINGLE|UNKNOWN",
            "relation_expression": "例如 (Apache-2.0 OR LGPL-2.1-or-later)",
            "evidence_quote": "最短充分原文或空字符串",
            "evidence_location": "pom_header|all_declared_licenses|none",
            "reasoning_summary": "简短中文说明", "confidence": "0到1",
            "needs_human_review": "boolean", "human_review_reasons": ["原因"],
        },
    }
    error = None
    for attempt in range(args.retries + 1):
        try:
            result = call_model(args.api_url, args.api_key, args.model, payload,
                                args.timeout, args.max_tokens)
            return normalize_result(item, result, rid, args.model, truncated or license_truncated)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt < args.retries:
                time.sleep(min(2 ** attempt, 8))
    return {"record_id": rid, "purl": item.get("purl"), "version": item.get("version"),
            "processing_status": "error", "error": error, "model": args.model}


def normalize_result(item: dict[str, Any], result: dict[str, Any], rid: str,
                     model: str, truncated: bool) -> dict[str, Any]:
    license_text = str(item.get("license_text", ""))
    pom_text = str(item.get("pom_text", ""))
    names = extract_license_names(license_text) or extract_license_names(pom_text)
    basis = str(result.get("license_basis", "unknown"))
    if basis not in {"pom_header", "all_declared_licenses_fallback", "unknown"}:
        basis = "unknown"
    effective_names = result.get("effective_license_names") or result.get("license_names") or names
    # normalized 为空时回退到 effective，避免 UNKNOWN 等记录里许可证名丢失
    normalized_names = result.get("normalized_license_names") or effective_names
    relation = str(result.get("license_relation", "UNKNOWN")).upper()
    if relation not in RELATIONS:
        relation = "UNKNOWN"
    quote = str(result.get("evidence_quote", "")).strip()
    evidence_valid = not quote or quote in license_text or quote in pom_text
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    review_reasons = result.get("human_review_reasons", [])
    if not isinstance(review_reasons, list):
        review_reasons = [str(review_reasons)]
    if quote and not evidence_valid:
        review_reasons.append("模型返回的证据不是输入原文的逐字子串")
    if truncated:
        review_reasons.append("送入模型的 POM 许可证相关文本被截断")
    needs_review = bool(result.get("needs_human_review", False)) or confidence < 0.8 or not evidence_valid or relation == "UNKNOWN"
    return {
        "record_id": rid,
        "purl": item.get("purl"),
        "version": item.get("version"),
        "source": item.get("source"),
        "license_source": item.get("license_source"),
        "license_basis": basis,
        "license_names": effective_names,
        "effective_license_names": effective_names,
        "all_declared_license_names": names,
        "normalized_license_names": normalized_names,
        "excluded_license_names": result.get("excluded_license_names", []),
        "exclusion_reason": result.get("exclusion_reason", ""),
        "license_relation": relation,
        "relation_expression": result.get("relation_expression", ""),
        "evidence_quote": quote if evidence_valid else "",
        "evidence_location": result.get("evidence_location", ""),
        "reasoning_summary": result.get("reasoning_summary", ""),
        "confidence": confidence,
        "needs_human_review": needs_review,
        "human_review_reasons": list(dict.fromkeys(str(x) for x in review_reasons if x)),
        "processing_status": "success",
        "model": model,
        "prompt_version": "license-relation-v2-component-header-priority",
        "input_truncated": truncated,
        "usage": result.get("_usage", {}),
    }


def load_completed(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
                if row.get("processing_status") == "success":
                    done.add(row["record_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description="使用大模型判断 Maven 多许可证关系")
    ap.add_argument("input", type=Path, nargs="?", default=INPUT_FILE,
                    help=f"输入 JSON 文件（默认 {INPUT_FILE}）")
    ap.add_argument("-o", "--output", type=Path, default=OUTPUT_FILE,
                    help=f"输出 JSONL 文件（默认 {OUTPUT_FILE}）")
    ap.add_argument("--model", default=DEEPSEEK_MODEL)
    ap.add_argument("--api-url", default=DEEPSEEK_API_URL)
    ap.add_argument("--api-key", default=DEEPSEEK_API_KEY)
    ap.add_argument("--max-license-chars", type=int, default=8000,
                    help="送入模型的 license_text 最大字符数，默认 8000")
    ap.add_argument("--max-pom-chars", type=int, default=12000,
                    help="送入模型的 POM 上下文最大字符数，默认 12000")
    ap.add_argument("--max-single-chars", type=int, default=100,
                    help="单许可证 license_text 的最大字符数；无 <> 标签且超过该长度才视为多许可证，默认 100")
    ap.add_argument("--max-records", type=int, help="仅处理前 N 条，便于抽样测试")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--max-tokens", type=int, default=4096, help="模型最大输出 Token，默认 4096")
    ap.add_argument("--workers", type=int, default=5, help="并发请求数，默认 5")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args()
    if not args.api_key:
        ap.error("请设置 DEEPSEEK_API_KEY 或传入 --api-key（不建议在命令历史中传密钥）")

    done = set() if args.no_resume else load_completed(args.output)
    processed = 0
    single_count = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8", buffering=1) as out, \
            ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        batch: list[tuple[int, dict[str, Any]]] = []
        scheduled = 0
        for index, item in enumerate(stream_json_array(args.input), 1):
            if record_id(item) in done:
                continue
            if not is_multi_license(item, args.max_single_chars):
                # 单许可证：直接判定为 SINGLE 写入结果，不调用 LLM
                out.write(json.dumps(single_license_result(item, args.model), ensure_ascii=False) + "\n")
                single_count += 1
                continue
            batch.append((index, item))
            scheduled += 1
            is_last = args.max_records is not None and scheduled >= args.max_records
            if len(batch) < max(1, args.workers) * 2 and not is_last:
                continue
            futures = {pool.submit(classify_one, value, args): (source_index, value)
                       for source_index, value in batch}
            for future in as_completed(futures):
                source_index, value = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = {"record_id": record_id(value), "purl": value.get("purl"),
                           "version": value.get("version"), "processing_status": "error",
                           "error": f"worker error: {type(exc).__name__}: {exc}", "model": args.model}
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                processed += 1
                print(f"[{processed}; input={source_index}] {value.get('purl')} -> {row['processing_status']}", file=sys.stderr)
            batch.clear()
            if is_last:
                break
        if batch:
            futures = {pool.submit(classify_one, value, args): (source_index, value)
                       for source_index, value in batch}
            for future in as_completed(futures):
                source_index, value = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = {"record_id": record_id(value), "purl": value.get("purl"),
                           "version": value.get("version"), "processing_status": "error",
                           "error": f"worker error: {type(exc).__name__}: {exc}", "model": args.model}
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                processed += 1
                print(f"[{processed}; input={source_index}] {value.get('purl')} -> {row['processing_status']}", file=sys.stderr)
    print(f"本次写入 {processed + single_count} 条（多许可证 LLM 判断 {processed} 条，单许可证直接判定 {single_count} 条）：{args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
