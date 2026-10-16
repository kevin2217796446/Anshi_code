"""测试运行日志记录器。比对 LLM 输出与 ground truth，生成详细报告。"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LOG_DIR = Path(__file__).parent / "数据" / "模型测试"
SKILL_VERSION = "1.0"


def _normalize_expression(expr: str) -> str:
    """标准化表达式以便比对（去除空格差异）。"""
    return expr.strip().replace(" ", "")


def generate_log(
    results: list[dict],
    ground_truth: dict[str, str],
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_time: float = 0,
    extra_params: dict | None = None,
    output_dir: Path | None = None,
) -> Path:
    """
    比对 LLM 结果与 ground truth，生成日志文件。

    results: [{"license": str, "license_spdx_name": str}, ...]
    ground_truth: {license_text: expected_spdx_name}
    output_dir: 输出目录，默认 LOG_DIR
    """
    timestamp = datetime.now()
    safe_model = model.replace("/", "_").replace(":", "_")
    log_name = f"test_log.json"
    log_dir = output_dir or LOG_DIR
    log_path = log_dir / log_name

    correct = 0
    errors = []
    total = len(results)

    for item in results:
        lic = item["license"]
        predicted = _normalize_expression(item.get("license_spdx_name", "none"))
        expected_raw = ground_truth.get(lic, "")
        expected = _normalize_expression(expected_raw)

        if predicted == expected:
            correct += 1
        else:
            errors.append({
                "license": lic,
                "expected": expected_raw,
                "predicted": item.get("license_spdx_name", "none"),
            })

    error_rate = round((total - correct) / total * 100, 1) if total > 0 else 100.0

    log = {
        "timestamp": timestamp.isoformat(),
        "model": model,
        "model_provider": os.getenv("LLM_DEEPSEEK_BASE_URL", "unknown"),
        "skill_version": SKILL_VERSION,
        "parameters": {
            "temperature": extra_params.get("temperature", 1.0) if extra_params else 1.0,
            "max_tokens": extra_params.get("max_tokens", 10000) if extra_params else 10000,
            "top_p": extra_params.get("top_p", 0.95) if extra_params else 0.95,
            "reasoning_effort": extra_params.get("reasoning_effort", "high") if extra_params else "N/A",
        },
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "results": {
            "total": total,
            "correct": correct,
            "error_rate_pct": error_rate,
            "errors": errors,
        },
        "prompt_template": "LICENSE_NORMALIZER_SKILL v1.0 (详见 LICENSE_NORMALIZER_SKILL.md)",
        "total_time_seconds": round(total_time, 1),
    }

    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"测试报告已生成: {log_path}")
    print(f"  模型: {model}")
    print(f"  准确率: {correct}/{total} = {100-error_rate:.1f}%")
    print(f"  错误率: {error_rate}%  ({len(errors)} 条)")
    print(f"  Token: {prompt_tokens + completion_tokens} (prompt={prompt_tokens}, completion={completion_tokens})")
    print(f"  耗时: {total_time:.1f}s")
    if errors:
        print(f"\n  错误详情 (前 5 条):")
        for e in errors[:5]:
            print(f"    输入: {e['license'][:60]}")
            print(f"    期望: {e['expected']}")
            print(f"    实际: {e['predicted']}")
            print()
    print(f"{'='*60}")

    return log_path
