"""定义自由模式使用的最小只读 Story 投影。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any


# Free Seed 只描述自由演绎的公开开场，不承载作者剧情图或正式账本。
FREE_SEED_SCHEMA_VERSION = "neko_theater_free_seed_v1"

# 统一限制 Free Seed 的根字段，避免完整 Story 被误当成自由模式输入继续传递。
_FREE_SEED_FIELDS = frozenset(
    {
        "schema_version",
        "source_story_id",
        "source_story_revision",
        "title",
        "theme",
        "background",
        "scenario_card",
        "opening_scene",
        "restrictions",
        "runtime_guardrails",
        "seed",
    }
)


class FreeSeedContractError(ValueError):
    """表示自由模式种子缺少必要字段或混入了作者图字段。"""  # noqa: DOCSTRING_CJK


def story_revision(story: dict[str, Any]) -> str:
    """返回来源 Story 的稳定 revision；旧合法包缺省时使用内容哈希。"""  # noqa: DOCSTRING_CJK
    explicit_revision = str(story.get("story_revision") or "").strip()
    if explicit_revision:
        return explicit_revision
    canonical_story = deepcopy(story)
    canonical_story.pop("story_revision", None)
    canonical = json.dumps(
        canonical_story,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"content-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def build_free_seed(
    story: dict[str, Any], scene: dict[str, Any]
) -> dict[str, Any]:
    """从已校验的完整 Story 和当前初始 Scene 构造最小自由模式输入。"""  # noqa: DOCSTRING_CJK
    scenario_card = story.get("scenario_card")
    normalized_card = scenario_card if isinstance(scenario_card, dict) else {}
    raw_seed = story.get("seed")
    normalized_seed = raw_seed if isinstance(raw_seed, dict) else {}
    forbidden_assumptions = normalized_seed.get("forbidden_assumptions")
    if not isinstance(forbidden_assumptions, list):
        forbidden_assumptions = []
    runtime_guardrails = story.get("runtime_guardrails")
    if not isinstance(runtime_guardrails, dict):
        runtime_guardrails = {}

    # 只复制自由 Actor 必需的公开字段，禁止把完整 Story 作为浅层副本带入模型。
    free_seed = {
        "schema_version": FREE_SEED_SCHEMA_VERSION,
        "source_story_id": str(story.get("id") or ""),
        "source_story_revision": story_revision(story),
        "title": str(story.get("title") or ""),
        "theme": str(story.get("theme") or ""),
        "background": str(story.get("background") or ""),
        "scenario_card": {
            "player_role": str(
                normalized_card.get("player_role") or "故事参与者"
            ),
            "catgirl_role": str(
                normalized_card.get("catgirl_role") or "当前故事中的共同主角"
            ),
            "primary_goal": str(normalized_card.get("primary_goal") or ""),
        },
        "opening_scene": {
            "id": str(scene.get("id") or ""),
            "title": str(scene.get("title") or ""),
            "text": str(scene.get("text") or ""),
        },
        "restrictions": deepcopy(story.get("restrictions") or []),
        "runtime_guardrails": deepcopy(runtime_guardrails),
        "seed": {
            "forbidden_assumptions": deepcopy(forbidden_assumptions),
        },
    }
    return validate_free_seed(free_seed)


def validate_free_seed(seed: dict[str, Any]) -> dict[str, Any]:
    """校验并复制 Free Seed，确保自由 Actor 不会收到完整作者图。"""  # noqa: DOCSTRING_CJK
    if not isinstance(seed, dict):
        raise FreeSeedContractError("自由模式种子必须是对象")
    unknown_fields = set(seed) - _FREE_SEED_FIELDS
    if unknown_fields:
        raise FreeSeedContractError("自由模式种子包含未声明字段")
    if seed.get("schema_version") != FREE_SEED_SCHEMA_VERSION:
        raise FreeSeedContractError("自由模式种子版本不受支持")
    for field in ("source_story_id", "source_story_revision", "title"):
        if not str(seed.get(field) or "").strip():
            raise FreeSeedContractError(f"自由模式种子缺少 {field}")

    scenario_card = seed.get("scenario_card")
    if not isinstance(scenario_card, dict):
        raise FreeSeedContractError("自由模式种子缺少 scenario_card")
    for field in ("player_role", "catgirl_role"):
        if field not in scenario_card or not isinstance(scenario_card[field], str):
            raise FreeSeedContractError(f"自由模式种子 scenario_card 缺少 {field}")
    # 自由聊天不一定需要作者目标；缺省时使用空字符串，交给提示词采用自然交流。
    if "primary_goal" in scenario_card and not isinstance(
        scenario_card["primary_goal"], str
    ):
        raise FreeSeedContractError(
            "自由模式种子 scenario_card 的 primary_goal 必须是字符串"
        )

    opening_scene = seed.get("opening_scene")
    if not isinstance(opening_scene, dict):
        raise FreeSeedContractError("自由模式种子缺少 opening_scene")
    for field in ("id", "title", "text"):
        if not str(opening_scene.get(field) or "").strip():
            raise FreeSeedContractError(f"自由模式种子 opening_scene 缺少 {field}")

    restrictions = seed.get("restrictions")
    if not isinstance(restrictions, list):
        raise FreeSeedContractError("自由模式种子的 restrictions 必须是数组")
    guardrails = seed.get("runtime_guardrails")
    if not isinstance(guardrails, dict):
        raise FreeSeedContractError(
            "自由模式种子的 runtime_guardrails 必须是对象"
        )
    forbidden = seed.get("seed")
    if not isinstance(forbidden, dict) or not isinstance(
        forbidden.get("forbidden_assumptions"), list
    ):
        raise FreeSeedContractError(
            "自由模式种子的 forbidden_assumptions 必须是数组"
        )

    # 返回深拷贝，避免调用方修改校验输入后影响来源 Story 或已保存 Session。
    return deepcopy(seed)
