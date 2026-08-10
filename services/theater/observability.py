"""记录自由模式模型调用的脱敏低基数指标。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import math
import threading
from collections import Counter, deque
from contextlib import suppress
from time import perf_counter
from typing import Any

from utils.instrument import counter, histogram


# 只保留当前自由模式职责，Numeric v2 Actor 使用自己的调用观测边界。
_CALL_TYPES = frozenset({"theater_free_actor"})
_SURFACES = frozenset({"opening", "free_input"})
_RESULT_KINDS = frozenset({"generation"})
_CALL_STATUSES = frozenset({"success", "timeout", "error"})
_FALLBACK_OUTCOMES = frozenset(
    {"context_incomplete", "model_config_missing", "model_call_failed", "invalid_model_output"}
)
_MAX_EVALUATION_SAMPLES = 4096

_lock = threading.Lock()
_call_samples: deque[dict[str, Any]] = deque(maxlen=_MAX_EVALUATION_SAMPLES)
_result_samples: deque[dict[str, str]] = deque(maxlen=_MAX_EVALUATION_SAMPLES)


def start_timer() -> float:
    """返回单调时钟起点，避免系统时间调整污染模型耗时。"""  # noqa: DOCSTRING_CJK
    return perf_counter()


def elapsed_ms(started_at: float) -> float:
    """把单调时钟起点转换为非负毫秒。"""  # noqa: DOCSTRING_CJK
    return max(0.0, (perf_counter() - started_at) * 1000.0)


def record_model_call(
    *,
    call_type: str,
    surface: str,
    started_at: float,
    status: str,
    response: Any | None = None,
) -> None:
    """记录模型传输结果，只提取 token 数值，不保存正文。"""  # noqa: DOCSTRING_CJK
    safe_call_type = call_type if call_type in _CALL_TYPES else "theater_free_actor"
    safe_surface = surface if surface in _SURFACES else "free_input"
    safe_status = status if status in _CALL_STATUSES else "error"
    input_tokens, output_tokens, total_tokens = _token_usage(response)
    duration_ms = elapsed_ms(started_at)
    sample = {
        "call_type": safe_call_type,
        "surface": safe_surface,
        "status": safe_status,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    with _lock:
        _call_samples.append(sample)
    with suppress(Exception):
        counter(
            "theater_llm_call",
            1,
            responsibility=safe_call_type,
            surface=safe_surface,
            status=safe_status,
        )
        histogram(
            "theater_llm_latency_ms",
            duration_ms,
            responsibility=safe_call_type,
            surface=safe_surface,
        )


def record_result(
    *, responsibility: str, surface: str, result_kind: str, outcome: str
) -> None:
    """记录自由模式合同结果，只接受固定职责和原因码。"""  # noqa: DOCSTRING_CJK
    sample = {
        "responsibility": responsibility if responsibility in _CALL_TYPES else "theater_free_actor",
        "surface": surface if surface in _SURFACES else "free_input",
        "result_kind": result_kind if result_kind in _RESULT_KINDS else "generation",
        "outcome": outcome if _is_safe_outcome(outcome) else "unknown",
    }
    with _lock:
        _result_samples.append(sample)
    with suppress(Exception):
        counter(
            "theater_llm_result",
            1,
            responsibility=sample["responsibility"],
            surface=sample["surface"],
            result_kind=sample["result_kind"],
            outcome=sample["outcome"],
        )


def reset_evaluation_window() -> None:
    """清空当前进程的评测窗口，不触碰生产 Session。"""  # noqa: DOCSTRING_CJK
    with _lock:
        _call_samples.clear()
        _result_samples.clear()


def evaluation_report() -> dict[str, Any]:
    """导出自由模式职责、场景和回退率聚合，不包含故事正文。"""  # noqa: DOCSTRING_CJK
    with _lock:
        calls = [dict(item) for item in _call_samples]
        results = [dict(item) for item in _result_samples]
    return {
        "schema_version": 1,
        "sample_count": len(calls),
        "by_call_type": _group_calls(calls, "call_type", _CALL_TYPES),
        "by_surface": _group_calls(calls, "surface", _SURFACES),
        "result_counts": _group_results(results),
        "rates": {
            "fallback_rate": _ratio(
                sum(item["outcome"] in _FALLBACK_OUTCOMES for item in results),
                len(results),
            )
        },
        "privacy": "aggregates_only_no_story_or_model_text",
    }


def _token_usage(response: Any | None) -> tuple[int, int, int]:
    """兼容供应商响应中的 token 字段，不读取消息正文。"""  # noqa: DOCSTRING_CJK
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        metadata = getattr(response, "response_metadata", None)
        usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0
    input_tokens = _nonnegative_int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
    output_tokens = _nonnegative_int(usage.get("output_tokens", usage.get("completion_tokens", 0)))
    total_tokens = _nonnegative_int(usage.get("total_tokens")) or input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def _nonnegative_int(value: Any) -> int:
    """把供应商数值收窄为安全的非负整数。"""  # noqa: DOCSTRING_CJK
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_safe_outcome(value: Any) -> bool:
    """限制 outcome 为短 ASCII snake_case 原因码。"""  # noqa: DOCSTRING_CJK
    if not isinstance(value, str) or not value or len(value) > 48:
        return False
    return all(char == "_" or "a" <= char <= "z" or "0" <= char <= "9" for char in value)


def _group_calls(
    samples: list[dict[str, Any]], key: str, expected_groups: frozenset[str]
) -> dict[str, dict[str, Any]]:
    """按职责或场景聚合调用量和耗时分位数。"""  # noqa: DOCSTRING_CJK
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(str(sample[key]), []).append(sample)
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(expected_groups | grouped.keys()):
        items = grouped.get(name, [])
        durations = sorted(float(item["duration_ms"]) for item in items)
        result[name] = {
            "calls": len(items),
            "input_tokens": sum(int(item["input_tokens"]) for item in items),
            "output_tokens": sum(int(item["output_tokens"]) for item in items),
            "total_tokens": sum(int(item["total_tokens"]) for item in items),
            "p50_ms": round(_percentile(durations, 0.50), 2),
            "p95_ms": round(_percentile(durations, 0.95), 2),
            "statuses": dict(Counter(str(item["status"]) for item in items)),
        }
    return result


def _group_results(samples: list[dict[str, str]]) -> dict[str, dict[str, int]]:
    """按结果类别导出原因码计数。"""  # noqa: DOCSTRING_CJK
    grouped: dict[str, Counter[str]] = {}
    for sample in samples:
        key = f"{sample['result_kind']}:{sample['surface']}"
        grouped.setdefault(key, Counter())[sample["outcome"]] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(grouped.items())}


def _ratio(numerator: int, denominator: int) -> float:
    """无样本时返回 0。"""  # noqa: DOCSTRING_CJK
    return round(numerator / denominator, 4) if denominator else 0.0


def _percentile(sorted_values: list[float], quantile: float) -> float:
    """使用线性插值计算小样本 P50/P95。"""  # noqa: DOCSTRING_CJK
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight
