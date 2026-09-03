"""Numeric v2 Actor 单回合上下文预算档位。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from typing import Final


NUMERIC_V2_DEFAULT_ACTOR_BUDGET_PROFILE: Final = "balanced"
NUMERIC_V2_ACTOR_BUDGET_PROFILES: Final = {
    "economy": {
        "input_max_tokens": 6000,
        "history_max_tokens": 1800,
        "history_max_turns": 6,
        "continuity_max_tokens": 900,
    },
    "balanced": {
        "input_max_tokens": 10000,
        "history_max_tokens": 5200,
        "history_max_turns": 12,
        "continuity_max_tokens": 1600,
    },
    "quality": {
        "input_max_tokens": 16000,
        "history_max_tokens": 10000,
        "history_max_turns": 20,
        "continuity_max_tokens": 2600,
    },
}


def numeric_v2_actor_budget(profile: object) -> dict[str, int]:
    """返回白名单档位；调用方必须显式处理非法值。"""  # noqa: DOCSTRING_CJK

    selected = NUMERIC_V2_ACTOR_BUDGET_PROFILES.get(str(profile or ""))
    if selected is None:
        raise ValueError("numeric_actor_budget_profile_invalid")
    return dict(selected)
