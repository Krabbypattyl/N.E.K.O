"""跨生成器与运行时同步的叙事时间锚点合同。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import re
from typing import Any


TIME_ANCHOR_CONTRACT_VERSION = "time_anchor_v1"
_DAY_RE = re.compile(r"(?P<number>三十一|三十|31|30)\s*(?:天|日)")
_DEADLINE_MARKERS = (
    "停运",
    "关闭",
    "闭站",
    "期限",
    "合同",
    "临时",
    "倒计时",
    "剩下",
    "最后",
)


def numeric_time_anchor_issues(value: Any, path: str = "story") -> list[dict[str, str]]:
    """拒绝同一 Story 在明确期限语境下混用 30 天和 31 天。

    这是 Compile 级确定性检查，不解释开放式时间表达；只有文本同时出现
    不同天数且至少一个上下文包含停运/合同等期限标记时才阻断。
    """
    occurrences: list[tuple[int, str, str]] = []

    def visit(item: Any, item_path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                visit(child, f"{item_path}.{key}" if item_path else str(key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{item_path}[{index}]")
        elif isinstance(item, str):
            for match in _DAY_RE.finditer(item):
                raw = match.group("number")
                number = 31 if raw == "三十一" else 30
                context = item[max(0, match.start() - 28) : match.end() + 28]
                occurrences.append((number, context, item_path))

    visit(value, path)
    numbers = {number for number, _context, _item_path in occurrences}
    if len(numbers) < 2:
        return []
    marked = [item for item in occurrences if any(marker in item[1] for marker in _DEADLINE_MARKERS)]
    if not marked:
        return []
    first_path = marked[0][2] or path or "story"
    return [{
        "code": "inconsistent_time_anchor",
        "path": first_path,
        "message": "同一期限语境不能同时使用 30 天和 31 天；请统一到 time_anchor_v1 的单一事实。",
    }]
