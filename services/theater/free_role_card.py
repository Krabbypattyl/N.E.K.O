"""定义自由模式使用的临时角色卡上下文，不写入全局角色配置。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .name_projection import replace_names


# 自由角色卡只属于当前 Free Session，和普通猫娘角色卡、Story Package 分开版本化。
FREE_ROLE_CARD_SCHEMA_VERSION = "neko_theater_free_role_card_v1"
_ROLE_CARD_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "description",
        "personality",
        "first_mes",
        "scenario",
        "mes_example",
        "world_info",
        "player_address",
        "player_role",
        "story_title",
        "scenario_title",
    }
)
_MAX_FIELD_CHARS = 24000
_MAX_WORLD_INFO_ITEMS = 12
_MAX_WORLD_INFO_CHARS = 12000


class FreeRoleCardContractError(ValueError):
    """表示临时角色卡不满足自由 Session 的最小输入合同。"""  # noqa: DOCSTRING_CJK


def validate_role_card(
    role_card: dict[str, Any],
    *,
    expected_name: str,
) -> dict[str, Any]:
    """校验并复制临时角色卡，禁止借此覆盖其他猫娘或写入作者剧情图。"""  # noqa: DOCSTRING_CJK
    if not isinstance(role_card, dict):
        raise FreeRoleCardContractError("自由角色卡必须是对象")
    unknown_fields = set(role_card) - _ROLE_CARD_FIELDS
    if unknown_fields:
        raise FreeRoleCardContractError("自由角色卡包含未声明字段")
    if role_card.get("schema_version") != FREE_ROLE_CARD_SCHEMA_VERSION:
        raise FreeRoleCardContractError("自由角色卡版本不受支持")

    normalized = deepcopy(role_card)
    card_name = str(normalized.get("name") or "").strip()
    current_name = str(expected_name or "").strip()
    if not card_name or card_name != current_name:
        # 角色名必须由服务端当前猫娘绑定，不能让请求借角色卡切换 N.E.K.O 角色。
        raise FreeRoleCardContractError("自由角色卡名称必须匹配当前猫娘")
    for field in ("description", "first_mes", "player_address", "player_role"):
        value = str(normalized.get(field) or "").strip()
        if not value:
            raise FreeRoleCardContractError(f"自由角色卡缺少 {field}")
        if len(value) > _MAX_FIELD_CHARS:
            raise FreeRoleCardContractError(f"自由角色卡 {field} 过长")
        normalized[field] = value

    for field in ("personality", "scenario", "mes_example", "story_title", "scenario_title"):
        value = str(normalized.get(field) or "").strip()
        if len(value) > _MAX_FIELD_CHARS:
            raise FreeRoleCardContractError(f"自由角色卡 {field} 过长")
        normalized[field] = value

    raw_world_info = normalized.get("world_info", [])
    if not isinstance(raw_world_info, list):
        raise FreeRoleCardContractError("自由角色卡 world_info 必须是数组")
    world_info: list[str] = []
    total_chars = 0
    for item in raw_world_info[:_MAX_WORLD_INFO_ITEMS]:
        value = str(item or "").strip()
        if not value:
            continue
        if total_chars + len(value) > _MAX_WORLD_INFO_CHARS:
            break
        world_info.append(value)
        total_chars += len(value)
    normalized["world_info"] = world_info
    return normalized


def bind_role_card_to_current_catgirl(
    role_card: dict[str, Any],
    *,
    expected_name: str,
    character_profile: str,
    player_address: str = "",
) -> dict[str, Any]:
    """把 RP-Hub 角色卡绑定到当前猫娘，只保留它的世界与演绎素材。

    RP-Hub 角色卡的主角是卡片作者定义的角色；N.E.K.O 自由模式的主角必须
    始终是当前猫娘。因此这里先替换主角和玩家称呼，再交给合同校验，避免
    原卡中的角色名、关系称呼或性格摘要继续污染当前猫娘。
    """  # noqa: DOCSTRING_CJK
    if not isinstance(role_card, dict):
        raise FreeRoleCardContractError("自由角色卡必须是对象")

    # 当前暂时兼容 RP-Hub 导出的 JSON 结构；最终角色卡格式确定后只替换这里的适配层，
    # 不让外部格式字段扩散到自由 Session、猫娘人格或剧本模式。
    source_card = role_card.get("data") if isinstance(role_card.get("data"), dict) else role_card
    # RP-Hub 导出的卡可能同时带有 worldInfo、character_book、正则脚本和 UI
    # 扩展；自由 Actor 只接收角色卡核心字段和世界书正文，其他字段在入口丢弃。
    normalized = {
        field: deepcopy(source_card.get(field))
        for field in _ROLE_CARD_FIELDS
        if field in source_card
    }
    normalized["schema_version"] = FREE_ROLE_CARD_SCHEMA_VERSION
    if not isinstance(normalized.get("world_info"), list):
        raw_world_info = source_card.get("worldInfo")
        if not isinstance(raw_world_info, list):
            character_book = source_card.get("character_book")
            raw_world_info = (
                character_book.get("entries")
                if isinstance(character_book, dict)
                else []
            )
        normalized["world_info"] = [
            str(item.get("content") or "").strip()
            if isinstance(item, dict)
            else str(item or "").strip()
            for item in raw_world_info
            if (
                str(item.get("content") or "").strip()
                if isinstance(item, dict)
                else str(item or "").strip()
            )
        ]
    current_name = str(expected_name or "").strip()
    if not current_name:
        raise FreeRoleCardContractError("当前猫娘名称不能为空")

    source_name = str(normalized.get("name") or "").strip()
    source_player_names = [
        str(normalized.get("player_address") or "").strip(),
        str(normalized.get("player_role") or "").strip(),
    ]
    current_player_address = str(player_address or "").strip()
    if not current_player_address:
        current_player_address = source_player_names[0] or source_player_names[1] or "玩家"

    # 先把原卡文本中的主角名和玩家身份替换掉，再覆盖结构化主角字段。
    # 这样 first_mes、scenario 和示例对白不会继续叫原角色“师兄”等旧称呼。
    replace_pairs = [(source_name, current_name)]
    replace_pairs.extend(
        (value, current_player_address)
        for value in source_player_names
        if value and value != current_player_address
    )
    replace_pairs = list(dict.fromkeys(replace_pairs))
    try:
        for field in ("first_mes", "scenario", "mes_example"):
            normalized[field] = replace_names(normalized.get(field), replace_pairs)
        world_info = normalized.get("world_info")
        if isinstance(world_info, list):
            rewritten_world_info: list[str] = []
            for item in world_info:
                rewritten_world_info.append(replace_names(item, replace_pairs))
            normalized["world_info"] = rewritten_world_info
    except ValueError as exc:
        if str(exc) == "conflicting_name_replacement":
            raise FreeRoleCardContractError("自由角色卡包含冲突的名称替换") from exc
        raise

    normalized["name"] = current_name
    # 当前猫娘的人格摘要是唯一主角人设来源；角色卡 description 只保留简短身份。
    normalized["description"] = f"{current_name}是当前猫娘，也是本次自由演绎的主角。"
    # 测试替身或维护态可能没有可读配置；此时保留卡片原人格，真实运行时
    # 只要服务端能读取当前猫娘，就会由当前人格摘要覆盖它。
    normalized["personality"] = str(
        character_profile or normalized.get("personality") or ""
    ).strip()
    normalized["player_address"] = current_player_address
    normalized["player_role"] = current_player_address
    return validate_role_card(normalized, expected_name=current_name)


def apply_role_card_to_seed(
    seed: dict[str, Any],
    role_card: dict[str, Any],
) -> dict[str, Any]:
    """把临时角色卡投影到 Free Seed，只替换自由公开开场，不改完整 Story。"""  # noqa: DOCSTRING_CJK
    projected = deepcopy(seed)
    projected["title"] = str(role_card.get("story_title") or projected.get("title") or "")
    projected["theme"] = str(role_card.get("scenario") or projected.get("theme") or "")
    projected["scenario_card"] = {
        "player_role": str(role_card.get("player_role") or "故事参与者"),
        "catgirl_role": str(role_card.get("description") or "当前猫娘"),
        # 角色卡不是作者剧情，不把 primary_goal 重新带入自由 Actor。
        "primary_goal": "",
    }
    projected["opening_scene"] = {
        "id": "free_role_card_opening",
        "title": str(role_card.get("scenario_title") or "角色卡开场"),
        "text": str(role_card.get("first_mes") or ""),
    }
    return projected
