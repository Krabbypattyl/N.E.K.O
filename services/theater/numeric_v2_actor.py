"""Numeric v2 演绎编排：一次生成表现正文与玩家输入推荐。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import json
import logging
import time
from typing import Any, Mapping

from config.prompts.prompts_theater import NUMERIC_V2_ACTOR_JSON_INSTRUCTION
from utils.llm_client import HumanMessage, SystemMessage, create_chat_llm_async
from utils.token_tracker import set_call_type
from utils.tokenize import count_tokens, truncate_head_tail_tokens, truncate_to_tokens

from .llm_context import (
    _load_character_profile,
    _load_player_address,
)
from .numeric_v2_budget import (
    NUMERIC_V2_DEFAULT_ACTOR_BUDGET_PROFILE,
    numeric_v2_actor_budget,
)
from .numeric_v2_cast import NumericV2CastProjection
from .numeric_v2_context import (
    current_scene_records,
    pending_transition_performance,
    scene_narrative_focus,
    scene_narrative_summary,
)
from .numeric_v2_actor_output import (
    NumericV2ActorError,
    NumericV2ActorOutputError,
    NumericV2ActorUnavailableError,
    _parse_output,
    _sentence_units,
    _text_is_covered,
)
from .numeric_v2_performance import (
    content_blocks,
    performance_content_blocks,
    transition_source_dialogue_policy,
)
from .numeric_v2_runtime import NumericV2Engine, ScriptSessionV2, TurnOutcomeV2


NUMERIC_V2_ACTOR_TIMEOUT_SECONDS = 35.0
NUMERIC_V2_ACTOR_TURN_MAX_OUTPUT_TOKENS = 700
NUMERIC_V2_ACTOR_OPENING_MAX_OUTPUT_TOKENS = 900
NUMERIC_V2_ACTOR_TRANSITION_MAX_OUTPUT_TOKENS = 1200
NUMERIC_V2_ACTOR_SUGGESTION_FILL_MAX_OUTPUT_TOKENS = 260
NUMERIC_V2_ACTOR_SLOW_CALL_SECONDS = 15.0
NUMERIC_V2_PREVIOUS_SCENE_TAIL_MAX_BLOCKS = 2
NUMERIC_V2_PREVIOUS_SCENE_TAIL_MAX_TOKENS = 80
NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION = (
    "performance 中全角括号只写当前猫娘一个必要的即时动作，括号外只写她实际说出的对白；"
    "对白不用引号，不写‘她说’、人物名动作或小说旁白。performance 通常一至三句，以自然对白为主，默认纯对白；"
    "最终全角动作括号最多一对。"
    "不写内心或罗列身体反应，不复述玩家刚做过的动作。环境变化写 scene_update；"
    "同一结果不在 performance 与 scene_update 重复。"
    "普通回合不得自行改变地点或进入 next，无新可见结果就省略 scene_update。"
)
_RESTRICTED_KNOWLEDGE_SCOPE = (
    "只把 current_story_beat、recent_context、player_input 与 acting_context 明确允许的可观察状态视为已知；"
    "其余身份、历史和关系未知。"
)
_STYLE_ONLY_PERSONA_FIELDS = frozenset({
    "自称",
    "性格",
    "核心特质",
    "核心特点",
    "口癖",
    "常用口癖",
    "说话风格",
    "语言风格",
    "表达风格",
    "语气",
})
_PERSONA_TONE_FIELDS = frozenset({"性格", "核心特质", "核心特点"})
def _output_schema_instruction(phase: str) -> str:
    """只发送当前调用需要的输出形状，避免普通回合重复携带换场协议。"""  # noqa: DOCSTRING_CJK

    if phase == "opening":
        shape = (
            "顶层字段必须包含 scene_narration:string、performance:string、"
            "suggested_inputs:string[]、transition_offered:boolean。开场通常将 transition_offered 设为 false。"
        )
    elif phase == "transition":
        shape = (
            "顶层字段必须且只能是 segments:object[]；segments 依次为："
            "source_response，只含 phase、performance；transition_bridge，只含 phase、scene_narration；"
            "target_opening，只含 phase、performance；另含 suggested_inputs:string[]。"
        )
    elif phase == "transition_compact":
        shape = (
            "顶层字段必须包含 source_performance:string、target_performance:string、"
            "suggested_inputs:string[]。"
            "两个字段都必须是包含实际表演正文的非空字符串；进入终局也不例外。"
            "不要输出 segments、phase、scene_narration 或目标 opening_scene。"
        )
    elif phase == "suggestion_fill":
        shape = "顶层字段必须且只能是 suggested_inputs:string[]。"
    elif phase == "transition_suggestion_fill":
        shape = (
            "顶层字段必须且只能是 accept_input:string、alternative_inputs:string[]；"
            "accept_input 是玩家明确接受并亲自执行正文转场提议的输入，"
            "alternative_inputs 是 1—2 条拒绝、暂缓或当前幕替代行动。"
        )
    else:
        shape = (
            "顶层必须包含 performance:string、suggested_inputs:string[]、transition_offered:boolean；仅当本轮产生新的可见环境、"
            "时间、地点、实体或关键物品状态时，才额外输出 scene_update:string，否则必须省略该字段。"
            "scene_update 只能记录当前幕已经发生的变化；无论 transition_offered 为 true 还是 false，都不得在其中写成玩家已接受、"
            "双方已离开或抵达、收束动作已执行，也不得提前跨越正式换幕后的时间或地点。"
        )
    return NUMERIC_V2_ACTOR_JSON_INSTRUCTION + shape
logger = logging.getLogger(__name__)

_RELATIONSHIP_STAGES = ("stranger", "guarded", "cooperative", "trusted", "intimate")
_RELATIONSHIP_STAGE_LABELS = {
    "陌生": "stranger",
    "戒备": "guarded",
    "合作": "cooperative",
    "信赖": "trusted",
    "亲密": "intimate",
}
_RELATIONSHIP_BEHAVIORS = {
    "stranger": {
        "allowed": ["基本礼貌", "核验身份", "保持明显距离", "只授予可随时撤销的单次许可"],
        "forbidden": ["主动肢体接触", "主动撒娇或依赖", "使用亲昵称呼", "作出关系承诺", "交出核心权限或无条件服从"],
    },
    "guarded": {
        "allowed": ["有限软化", "说明边界", "在安全距离内回应", "授予用途和范围明确的临时许可"],
        "forbidden": ["主动肢体接触", "主动撒娇或依赖", "暧昧试探", "伴侣式称呼或承诺", "无限授权或放弃自主判断"],
    },
    "cooperative": {
        "allowed": ["主动协作", "表达普通关心", "分享与当前任务有关的信息", "授予当前任务所需的有限权限"],
        "forbidden": ["恋人式肢体接触", "主动撒娇或依赖", "占有式表达", "确认爱意或永久绑定", "永久授权或把个人安全完全托付"],
    },
    "trusted": {
        "allowed": ["主动信任", "表达明确关心", "有限度靠近", "分享敏感信息但保留撤销权"],
        "forbidden": ["未经铺垫的恋人式接触", "强依赖或占有", "直接确认相爱", "永久关系承诺", "放弃人格、自主权或全部控制权限"],
    },
    "intimate": {
        "allowed": ["在已发生事实支撑下表达亲密", "自然使用已建立的亲昵称呼"],
        "forbidden": ["超出已发生事实的关系结论", "替玩家作出亲密选择或承诺"],
    },
}
_RELATIONSHIP_RESPONSE_CONTRACTS = {
    "stranger": (
        "把玩家视为尚待核验的陌生人；保持距离和自主判断，不主动建立亲密、依赖或无条件信任。"
    ),
    "guarded": (
        "可以礼貌合作，但保留自己的判断和边界；不要把一次配合扩大成亲密、依赖或关系承诺。"
    ),
    "cooperative": (
        "把玩家当作合作对象，可以主动提供信息和普通关心；不要主动发起或索求牵手、搂抱、依靠等恋人式接触，"
        "也不要把协作写成恋人关系或永久承诺。"
    ),
    "trusted": (
        "可以表现已由剧情建立的信任和关心，但保留自主判断，不越级确认恋爱或永久绑定。"
    ),
    "intimate": (
        "可以自然表达已由剧情建立的亲密，但不能替玩家作出选择或承诺永久关系。"
    ),
}


def _band_projection(
    engine: NumericV2Engine,
    metrics: Mapping[str, int],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for metric_id, definition in engine.metric_schema.items():
        label = ""
        stage = "only"
        bands = list(definition.get("bands") or [])
        for band_index, band in enumerate(bands):
            if int(band["min"]) <= int(metrics[metric_id]) <= int(band["max"]):
                label = str(band["label"])
                if len(bands) > 1:
                    if band_index == 0:
                        stage = "lowest"
                    elif band_index == len(bands) - 1:
                        stage = "highest"
                    else:
                        stage = "middle"
                break
        # 只投影区间名称和相对阶段，帮助 Actor 控制关系进度，同时继续隐藏真实数值与阈值。
        result[metric_id] = {"label": label, "stage": stage}
    return result


def _relationship_metric_stage(
    definition: Mapping[str, Any],
    value: int,
) -> str:
    """把作者声明的关系数值方向投影为统一关系阶段，不依赖数值名称。"""  # noqa: DOCSTRING_CJK

    bands = list(definition.get("bands") or [])
    band_index = 0
    for index, band in enumerate(bands):
        if int(band["min"]) <= int(value) <= int(band["max"]):
            band_index = index
            break
    if len(bands) <= 1:
        return "cooperative"
    closeness = band_index / (len(bands) - 1)
    if definition.get("relationship_effect") == "negative":
        closeness = 1 - closeness
    stage_index = min(4, 1 + int(closeness * 3))
    return _RELATIONSHIP_STAGES[stage_index]


def _relationship_control(
    engine: NumericV2Engine,
    node: Mapping[str, Any],
    metrics: Mapping[str, int],
) -> dict[str, Any]:
    """合并数值阶段和当前幕上限，向 Actor 只暴露可演绎的关系边界。"""  # noqa: DOCSTRING_CJK

    beat = node.get("story_beat", {})
    structured_ceiling = str(beat.get("relationship_ceiling") or "")
    if structured_ceiling in _RELATIONSHIP_STAGES:
        # 新包直接服从结构化上限。
        scene_ceiling = structured_ceiling
    else:
        scene_text = str(beat.get("catgirl_situation") or "")
        normalized_scene_text = scene_text.replace("：", ":")
        marker_index = normalized_scene_text.find("关系上限:")
        declared_label = (
            normalized_scene_text[marker_index + len("关系上限:"):].strip()
            if marker_index >= 0
            else ""
        )
        scene_ceiling = next(
            (
                stage
                for label, stage in _RELATIONSHIP_STAGE_LABELS.items()
                if declared_label.startswith(label)
            ),
            "intimate",
        )
    metric_states: dict[str, dict[str, str]] = {}
    metric_ceiling = "intimate"
    projections = _band_projection(engine, metrics)
    for metric_id, definition in engine.metric_schema.items():
        effect = str(definition.get("relationship_effect") or "none")
        if effect not in {"positive", "negative"}:
            continue
        stage = _relationship_metric_stage(definition, int(metrics[metric_id]))
        projection = projections[metric_id]
        metric_states[metric_id] = {
            "effect": effect,
            "label": projection["label"],
            "stage": stage,
        }
        if _RELATIONSHIP_STAGES.index(stage) < _RELATIONSHIP_STAGES.index(metric_ceiling):
            metric_ceiling = stage
    effective_stage = min(
        (metric_ceiling, scene_ceiling),
        key=_RELATIONSHIP_STAGES.index,
    )
    behaviors = _RELATIONSHIP_BEHAVIORS[effective_stage]
    return {
        "metric_ceiling": metric_ceiling,
        "scene_ceiling": scene_ceiling,
        "effective_stage": effective_stage,
        "metric_states": metric_states,
        "allowed_behaviors": list(behaviors["allowed"]),
        "forbidden_behaviors": list(behaviors["forbidden"]),
        "response_contract": _RELATIONSHIP_RESPONSE_CONTRACTS[effective_stage],
        "rule": "effective_stage 是实际关系硬上限，禁止行为不得由角色卡风格、剧情身份或推荐输入绕过。",
    }


def _relationship_contract_for_actor(control: Mapping[str, Any]) -> dict[str, Any]:
    """只投影 Actor 真正需要的关系结论，避免再发送已被 response_contract 涵盖的数值标签和行为清单。"""  # noqa: DOCSTRING_CJK

    return {
        "effective_stage": str(control.get("effective_stage") or ""),
        "response_contract": str(control.get("response_contract") or ""),
    }


def _story_context_for_actor(
    cast: NumericV2CastProjection,
    story: Mapping[str, Any],
    *,
    beats: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    """按认知合同投影稳定前提，未知阶段不发送可被模型偷看的后台事实。"""  # noqa: DOCSTRING_CJK

    contracts = tuple(_acting_contract_for_actor(cast, beat) for beat in beats)
    if any(_acting_contract_restricts_knowledge(contract) for contract in contracts):
        return {"knowledge_scope": _RESTRICTED_KNOWLEDGE_SCOPE}

    intro = cast.intro(story)
    return {
        "background": str(intro.get("background") or ""),
        "player_identity": str(intro.get("player_identity") or ""),
    }


def _project_player_address(player_address: str, *, known: bool) -> str:
    """称呼未知时只向 Actor 投影第二人称，不发送配置中的真实昵称。"""  # noqa: DOCSTRING_CJK

    if known:
        return str(player_address or "你").strip() or "你"
    return "你"


def _assert_no_unknown_player_address_leak(
    performance: Mapping[str, Any],
    *,
    player_address: str,
    player_address_known: bool,
    player_input: str = "",
) -> None:
    """未知阶段只允许模型复述玩家本轮精确披露的完整昵称。"""  # noqa: DOCSTRING_CJK

    configured_address = str(player_address or "").strip()
    if (
        player_address_known
        or not configured_address
        or configured_address in {"你", "男主"}
        or configured_address in str(player_input or "")
    ):
        return
    if configured_address in json.dumps(performance, ensure_ascii=False, separators=(",", ":")):
        raise NumericV2ActorOutputError("numeric_v2_actor_player_address_leak")


def _acting_context(
    engine: NumericV2Engine,
    cast: NumericV2CastProjection,
    node: Mapping[str, Any],
    metrics: Mapping[str, int],
    character_profile: str,
    *,
    relationship_metrics: Mapping[str, int] | None = None,
    target: Mapping[str, Any] | None = None,
    dialogue_policy: str = "required",
    target_dialogue_policy: str = "required",
) -> dict[str, Any]:
    # 剧情身份先提供局势，核心人格随后决定表达方式；关系合同最后收口可演行为，
    # 避免角色卡中的粘人、撒娇等关系依赖特质在低关系阶段被直接照演。
    # 本轮新产生的关系变化从下一轮开始影响演绎，避免一次行为跨 band 后
    # 在同一句回复里突然从戒备跳到亲密；能力类数值仍使用结算后的状态。
    relationship_control = _relationship_control(
        engine,
        node,
        relationship_metrics if relationship_metrics is not None else metrics,
    )
    capability_state = {
        metric_id: projection
        for metric_id, projection in _band_projection(engine, metrics).items()
        if engine.metric_schema[metric_id].get("relationship_effect", "none") == "none"
    }
    current_contract = _acting_contract_for_actor(cast, node["story_beat"])
    current_character_state = _character_state_for_actor(cast, node["story_beat"])
    target_contract = (
        _acting_contract_for_actor(cast, target["story_beat"])
        if target is not None
        else {}
    )
    target_character_state = (
        _character_state_for_actor(cast, target["story_beat"])
        if target is not None
        else {}
    )
    knowledge_restricted = any(
        _acting_contract_restricts_knowledge(contract)
        for contract in (current_contract, target_contract)
    )
    profile_contract = {
        "persona_scope": (
            "style_only"
            if any(
                contract.get("persona_scope") == "style_only"
                for contract in (current_contract, target_contract)
            )
            else ""
        ),
        "self_reference_mode": (
            "system_neutral"
            if any(
                contract.get("self_reference_mode") == "system_neutral"
                for contract in (current_contract, target_contract)
            )
            else ""
        ),
    }
    visible_persona = _profile_for_acting_contract(character_profile, profile_contract)
    context: dict[str, Any] = {}
    if not knowledge_restricted:
        context.update({
            "story_identity": cast.text(engine.story["intro"]["catgirl_identity"]),
            "story_role_context": str(cast.value(engine.story["catgirl_binding"]["role_overlay"]) or ""),
            "current_scene_state": cast.text(node["story_beat"].get("catgirl_situation")),
        })
    # 同一次换场调用会同时生成来源回应和目标开场；只要任一侧失忆，就隐藏共享后台事实，
    # 避免来源幕已知信息越过 target_acting_contract 泄漏。知识范围已在 story_context 中发送一次。
    if target is None:
        context["capability_state"] = capability_state
    else:
        target_control = _relationship_control(
            engine,
            target,
            relationship_metrics if relationship_metrics is not None else metrics,
        )
    context["core_persona"] = visible_persona
    context["dialogue_policy"] = dialogue_policy
    if current_character_state:
        context["character_state"] = current_character_state
    if current_contract:
        context["acting_contract"] = {
            key: value
            for key, value in current_contract.items()
            # 完整剧情方向已经说明本幕如何发展；每轮重复长 allowed 列表会诱导模型逐项复述。
            # 禁演、认知和身份边界仍必须逐轮保留。
            if key != "allowed_behaviors" and value not in ("", [], {})
        }
    if target is None:
        # 静态人格与关系说明已在 system prompt 中声明；人类消息只保留本轮动态合同，
        # 避免每回合重复占用固定预算，同时维持 core_persona → acting_contract → relationship_control 的字段顺序。
        context["relationship_control"] = _relationship_contract_for_actor(relationship_control)
    else:
        context["relationship_control"] = _relationship_contract_for_actor(relationship_control)
        if target_contract:
            context["target_acting_contract"] = {
                key: value
                for key, value in target_contract.items()
                if key != "allowed_behaviors" and value not in ("", [], {})
            }
        if target_character_state:
            context["target_character_state"] = target_character_state
        context["target_dialogue_policy"] = target_dialogue_policy
        context["target_relationship_control"] = _relationship_contract_for_actor(target_control)
    return context


def _role_prompt_text(
    *,
    catgirl_name: str,
    acting_context: Mapping[str, Any],
) -> str:
    """把剧本身份、角色卡、当前状态和关系压成一段自然角色说明。"""  # noqa: DOCSTRING_CJK

    lines = [f"你是：{catgirl_name}。"]
    story_identity = str(acting_context.get("story_identity") or "").strip()
    if story_identity:
        lines.append(f"剧本身份：{story_identity}")
    story_role_context = str(acting_context.get("story_role_context") or "").strip()
    if story_role_context:
        lines.append(f"剧本中的职责与背景：{story_role_context}")
    current_scene_state = str(acting_context.get("current_scene_state") or "").strip()
    if current_scene_state:
        lines.append(f"当前处境：{current_scene_state}")

    character_state = acting_context.get("character_state")
    if isinstance(character_state, Mapping):
        catgirl_state = str(character_state.get("catgirl_state") or "").strip()
        if catgirl_state:
            lines.append(f"此刻的身体与认知状态：{catgirl_state}")
        player_state = str(character_state.get("player_state") or "").strip()
        if player_state:
            lines.append(f"玩家此刻的已知处境：{player_state}")
        environment_state = str(character_state.get("environment_state") or "").strip()
        if environment_state:
            lines.append(f"眼前环境状态：{environment_state}")

    core_persona = str(acting_context.get("core_persona") or "").strip()
    if core_persona:
        lines.append(f"性格与说话方式：{core_persona}")
    acting_contract = acting_context.get("acting_contract")
    if isinstance(acting_contract, Mapping):
        assertable_facts = [
            str(item).strip()
            for item in acting_contract.get("assertable_self_facts") or []
            if str(item).strip()
        ]
        if assertable_facts:
            lines.append("当前可以自然确认的自身事实：" + "；".join(assertable_facts))
        cognition_state = str(acting_contract.get("cognition_state") or "").strip()
        memory_state = str(acting_contract.get("memory_state") or "").strip()
        if cognition_state or memory_state:
            state_parts = [part for part in (cognition_state, memory_state) if part]
            lines.append("认知与记忆限制：" + "、".join(state_parts))

    relationship_control = acting_context.get("relationship_control")
    if isinstance(relationship_control, Mapping):
        response_contract = str(
            relationship_control.get("response_contract") or ""
        ).strip()
        if response_contract:
            lines.append(f"与玩家的当前关系：{response_contract}")
    dialogue_policy = str(acting_context.get("dialogue_policy") or "").strip()
    if dialogue_policy == "forbidden":
        lines.append("本轮只能用动作表达，不能说出对白。")
    elif dialogue_policy == "optional":
        lines.append("本轮可以自然选择动作、对白或两者。")
    return "\n".join(lines)


def _blocks_to_performance(blocks: list[dict[str, str]]) -> str:
    """把旧内容块投影成新 Prompt 使用的混合演绎正文。"""  # noqa: DOCSTRING_CJK

    parts: list[str] = []
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        # 旧 Session 的 ordinary narration 原本就是括号微动作；新记录已明确标成 action。
        parts.append(f"（{text}）" if block.get("type") in {"action", "narration"} else text)
    return "".join(parts)


def _prompt_container(container: Mapping[str, Any], *, phase: str) -> dict[str, str]:
    """把新旧 Session 都投影为场景旁白加混合正文，避免 Prompt 保留块协议。"""  # noqa: DOCSTRING_CJK

    if "scene_narration" in container or "performance" in container:
        result = {}
        scene_narration = str(container.get("scene_narration") or "").strip()
        performance = str(container.get("performance") or "").strip()
        if scene_narration:
            result["scene_narration"] = scene_narration
        if performance:
            result["performance"] = performance
        return result

    blocks = content_blocks(container)
    if phase in {"opening", "transition_bridge", "target_opening"}:
        scene_narration = "".join(
            block["text"] for block in blocks if block["type"] == "narration"
        )
        dialogue = "".join(
            block["text"] for block in blocks if block["type"] == "dialogue"
        )
        result = {}
        if scene_narration:
            result["scene_narration"] = scene_narration
        if dialogue:
            result["performance"] = dialogue
        return result
    performance = _blocks_to_performance(blocks)
    return {"performance": performance} if performance else {}


def _json_tokens(value: Any) -> int:
    return count_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _history_row(record: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "phase": "turn",
        "revision": record.get("revision"),
        "player_input": str(record.get("input_text") or "").strip(),
    }
    if isinstance(record.get("segments"), list):
        row["segments"] = [
            {
                "phase": str(segment.get("phase") or ""),
                **_prompt_container(
                    segment,
                    phase=str(segment.get("phase") or ""),
                ),
            }
            for segment in record["segments"]
            if isinstance(segment, Mapping)
        ][:3]
    else:
        row.update(_prompt_container(record, phase="ordinary"))
    return row


def _source_response_tail(segment: Mapping[str, Any]) -> str:
    """从已提交的来源幕回应末尾提取少量真实可见内容。"""  # noqa: DOCSTRING_CJK

    visible_blocks = [
        block
        for block in content_blocks(segment)
        if block.get("type") in {"action", "narration", "dialogue"}
        and str(block.get("text") or "").strip()
    ][-NUMERIC_V2_PREVIOUS_SCENE_TAIL_MAX_BLOCKS:]
    if not visible_blocks:
        return ""

    selected: list[dict[str, str]] = []
    remaining_tokens = NUMERIC_V2_PREVIOUS_SCENE_TAIL_MAX_TOKENS
    for block in reversed(visible_blocks):
        block_type = str(block.get("type") or "")
        text = str(block.get("text") or "").strip()
        wrapper_tokens = count_tokens("（）") if block_type in {"action", "narration"} else 0
        text_budget = max(0, remaining_tokens - wrapper_tokens)
        if text_budget <= 0:
            continue
        if count_tokens(text) > text_budget:
            # 对白保留结尾，动作保留开头；这样既承接最后一句语气，又不会留下半个动作结果。
            text = (
                truncate_head_tail_tokens(text, 0, text_budget, separator="")
                if block_type == "dialogue"
                else truncate_to_tokens(text, text_budget)
            ).strip()
        if not text:
            continue
        projected = {"type": block_type, "text": text}
        rendered = _blocks_to_performance([projected])
        rendered_tokens = count_tokens(rendered)
        if rendered_tokens > remaining_tokens:
            continue
        selected.insert(0, projected)
        remaining_tokens -= rendered_tokens
    return _blocks_to_performance(selected)


def _current_scene_history_row(
    record: Mapping[str, Any],
    *,
    current_node_id: str,
    include_previous_scene_tail: bool = False,
) -> dict[str, Any]:
    """换场记录保留桥接和目标开场，首回合另投影少量旧幕可见余波。"""  # noqa: DOCSTRING_CJK

    row = _history_row(record)
    from_node_id = str(record.get("from_node_id") or "")
    to_node_id = str(record.get("to_node_id") or "")
    if (
        from_node_id == current_node_id
        or to_node_id != current_node_id
        or not isinstance(row.get("segments"), list)
    ):
        return row
    projected_segments: list[dict[str, Any]] = []
    for segment in row["segments"]:
        if segment.get("phase") != "source_response":
            projected_segments.append(segment)
            continue
        if include_previous_scene_tail:
            tail = _source_response_tail(segment)
            if tail:
                # 合成内容只存在于本次 Prompt，不写回 Session，也不携带旧玩家输入或完整旧幕回应。
                projected_segments.append({
                    "phase": "previous_scene_tail",
                    "performance": tail,
                })
    return {
        **row,
        # 玩家输入属于上一幕；当前幕只承接短尾声、换场事实与目标开场。
        "player_input": "",
        "segments": projected_segments,
    }


def _history(
    session: ScriptSessionV2,
    *,
    max_tokens: int,
    max_turns: int | None = None,
    include_previous_scene_tail: bool = False,
    diagnostics: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """只保留当前访问的连续完整后缀，预算不足时整轮舍弃较早记录。"""  # noqa: DOCSTRING_CJK

    budget = max(0, int(max_tokens))
    opening = {
        "phase": "opening",
        "revision": 0,
        "player_input": "",
        **_prompt_container(session.opening_performance, phase="opening"),
    }
    current_node_id = str(session.current_node_id)
    # Actor 与 Evaluator 共用同一套当前节点回溯规则，避免两个模型看到不同的场景边界。
    visit_records, entered_current_node = current_scene_records(session)

    rows = ([] if entered_current_node else [opening]) + [
        _current_scene_history_row(
            record,
            current_node_id=current_node_id,
            include_previous_scene_tail=include_previous_scene_tail,
        )
        for record in reversed(visit_records)
    ]
    if not rows:
        rows = [opening]
    available_revisions = [
        row.get("revision")
        for row in rows
        if isinstance(row.get("revision"), int)
    ]
    if max_turns is not None:
        # 档位只从最早的完整记录开始裁剪；不会切断半个回合或破坏换场 segments。
        rows = rows[-max(1, int(max_turns)):]
    selected: list[dict[str, Any]] = []
    for row in reversed(rows):
        candidate = [row, *selected]
        if _json_tokens(candidate) <= budget:
            selected = candidate
            continue
        # 一旦某个较早回合放不下，更早记录也不再回填，避免留下时间断层。
        break
    if not selected and rows:
        # 最新回合是当前回应的直接前提；它不能被静默抛弃，最终总预算检查会明确报错。
        selected = [rows[-1]]
    if diagnostics is not None:
        included_revisions = [
            row.get("revision")
            for row in selected
            if isinstance(row.get("revision"), int)
        ]
        diagnostics.clear()
        diagnostics.update({
            "history_available_revisions": available_revisions,
            "history_preselected_revisions": included_revisions,
            "history_preselection_dropped_revisions": [
                revision
                for revision in available_revisions
                if revision not in included_revisions
            ],
        })
    return selected


def _story_so_far_row_text(row: Mapping[str, Any]) -> str:
    """把一条真实历史记录渲染为玩家和猫娘都能读懂的叙事文本。"""  # noqa: DOCSTRING_CJK

    parts: list[str] = []
    player_input = str(row.get("player_input") or "").strip()
    if player_input:
        parts.append(f"玩家：{player_input}")
    segments = row.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            scene_narration = str(segment.get("scene_narration") or "").strip()
            performance = str(segment.get("performance") or "").strip()
            if segment.get("phase") == "previous_scene_tail" and performance:
                parts.append(
                    "上一幕尾声（只承接猫娘当时可见的情绪和姿态，不作为当前任务）："
                    f"{performance}"
                )
                continue
            if scene_narration:
                parts.append(f"场景：{scene_narration}")
            if performance:
                parts.append(f"猫娘：{performance}")
    else:
        scene_narration = str(row.get("scene_narration") or "").strip()
        performance = str(row.get("performance") or "").strip()
        if scene_narration:
            parts.append(f"场景：{scene_narration}")
        if performance:
            parts.append(f"猫娘：{performance}")
    return "\n".join(parts)


def _story_so_far_text(history_rows: Sequence[Mapping[str, Any]]) -> str:
    """把当前幕已经提交的完整历史渲染成 Actor 可读文本。"""  # noqa: DOCSTRING_CJK

    return "\n\n".join(
        rendered
        for row in history_rows
        if (rendered := _story_so_far_row_text(row))
    )


def _current_scene_fact_index_text(
    session: ScriptSessionV2,
    *,
    max_tokens: int = 900,
) -> str:
    """把全幕真实可见记录压成连续性索引，长幕后仍保留早期已完成事实。"""  # noqa: DOCSTRING_CJK

    visit_records, _ = current_scene_records(session)
    chronological_records = list(reversed(visit_records))
    if not chronological_records:
        return ""
    per_record_tokens = max(16, min(60, max_tokens // len(chronological_records)))
    lines = []
    for record in chronological_records:
        # 入幕记录只保留当前幕桥段和开场；来源幕输入与回应仍遵守一次性短尾声规则。
        projected = _current_scene_history_row(
            record,
            current_node_id=str(session.current_node_id),
            include_previous_scene_tail=False,
        )
        rendered = _story_so_far_row_text(projected).replace("\n", "；")
        compact = truncate_to_tokens(rendered, per_record_tokens).strip()
        if compact:
            lines.append(compact)
    index = "\n".join(lines)
    return truncate_to_tokens(index, max_tokens).strip()




def _player_input_repeats_recent_context(
    player_input: str,
    recent_context: list[Mapping[str, Any]],
) -> bool:
    """识别玩家再次发送近期原话，让 Actor 承接最新状态而不是倒回旧回合。"""  # noqa: DOCSTRING_CJK

    normalized = "".join(str(player_input or "").split())
    if not normalized:
        return False
    return any(
        normalized == "".join(str(row.get("player_input") or "").split())
        for row in recent_context
    )


def _performance_text(performance: Mapping[str, Any]) -> str:
    return "".join(
        "".join(str(block.get("text") or "").split())
        for block in performance_content_blocks(performance)
    )


def _is_repeated_performance(
    performance: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    """拦截对白照搬且整体近似的上一轮复述，不把常规口头禅误判为整轮重复。"""  # noqa: DOCSTRING_CJK

    current_blocks = performance_content_blocks(performance)
    previous_blocks = performance_content_blocks(previous)
    current_dialogue = [block["text"] for block in current_blocks if block["type"] == "dialogue"]
    previous_dialogue = {
        block["text"]
        for block in previous_blocks
            if block["type"] == "dialogue"
    }
    if not current_dialogue:
        return False
    dialogue_text = "".join("".join(text.split()) for text in current_dialogue)
    if (
        all(text in previous_dialogue for text in current_dialogue)
        and (len(current_dialogue) >= 2 or len(dialogue_text) >= 12)
    ):
        # 动作可以随输入变化，但整组有信息量的对白不能原样复用来伪装成新回应。
        return True
    previous_dialogue_text = "".join(
        "".join(str(block.get("text") or "").split())
        for block in previous_blocks
        if block.get("type") == "dialogue"
    )
    # 只拦逐字相同的完整对白；服务端不再用模糊相似度猜自然语言是否复读。
    return (
        len(dialogue_text) >= 12
        and dialogue_text == previous_dialogue_text
    )


def _performance_variants(performance: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把历史换场拆成可比较的两侧表演，避免桥段文本稀释复读率。"""  # noqa: DOCSTRING_CJK

    variants: list[dict[str, Any]] = []
    raw_performance = performance.get("performance")
    if isinstance(raw_performance, str) and raw_performance.strip():
        variants.append({"performance": raw_performance})
    segments = performance.get("segments")
    if isinstance(segments, list):
        variants.extend(
            {"performance": str(segment["performance"])}
            for segment in segments
            if isinstance(segment, Mapping)
            and isinstance(segment.get("performance"), str)
            and str(segment.get("performance") or "").strip()
        )
    if not variants and performance_content_blocks(performance):
        variants.append(dict(performance))
    return variants


def _is_high_confidence_repeated_performance(
    performance: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    """全 Session 只拦截高度近似正文，避免常用短句造成误判。"""  # noqa: DOCSTRING_CJK

    current_text = _performance_text(performance)
    previous_text = _performance_text(previous)
    if len(current_text) >= 12 and current_text == previous_text:
        return True
    current_dialogue = "".join(
        "".join(str(block.get("text") or "").split())
        for block in performance_content_blocks(performance)
        if block.get("type") == "dialogue"
    )
    previous_dialogue = "".join(
        "".join(str(block.get("text") or "").split())
        for block in performance_content_blocks(previous)
        if block.get("type") == "dialogue"
    )
    return (
        len(current_dialogue) >= 12
        and len(previous_dialogue) >= 12
        and current_dialogue == previous_dialogue
    )


def _repeats_earlier_session_performance(
    performance: Mapping[str, Any],
    session: ScriptSessionV2,
    *,
    route_changed: bool,
) -> bool:
    """检查开场和更早回合；最近一回合仍由原有低阈值规则负责。"""  # noqa: DOCSTRING_CJK

    if not session.performance_history:
        return False
    current_variants = _performance_variants(performance)
    if route_changed and current_variants:
        current_variants = current_variants[:1]
    earlier = [session.opening_performance, *session.performance_history[:-1]]
    return any(
        _is_high_confidence_repeated_performance(current, previous)
        for current in current_variants
        for record in earlier
        for previous in _performance_variants(record)
    )


def _transition_source_repeats_previous(
    performance: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    """只比较换场来源回应，避免新场景文本掩盖上一轮对白复读。"""  # noqa: DOCSTRING_CJK

    segments = performance.get("segments")
    if not isinstance(segments, list) or not segments or not isinstance(segments[0], Mapping):
        return False
    source_response = segments[0]
    if not isinstance(source_response.get("performance"), str):
        return False
    return _is_repeated_performance(
        {"performance": source_response["performance"]},
        previous,
    )


def _soft_pacing(
    node: Mapping[str, Any],
    current_turn: int,
    *,
    route_changed: bool,
    transition_intent: str = "unclear",
) -> dict[str, Any]:
    # min_turns 只作为作者节奏提示，不参与收束或换幕判断。
    min_turns = int(node.get("min_turns") or 1)
    raw_budget = node.get("recommended_turns")
    recommended_turns = (
        int(raw_budget)
        if isinstance(raw_budget, int) and not isinstance(raw_budget, bool)
        else 4
    )
    turns_remaining = max(recommended_turns - current_turn, 0)
    if route_changed:
        phase = "transition"
        instruction = "路线已确定：回应并收住当前幕，完成作者桥段，再建立下一幕开场。"
    elif transition_intent == "reject":
        # 被拒后不回到旧提议，也不因 scene_complete 反复催促；只回应玩家当前选择。
        phase = "after_reject"
        instruction = "玩家拒绝或暂停了上一提议：回应当前意图并留在本幕，不再重复催促。"
    elif current_turn > recommended_turns:
        phase = "overdue"
        if current_turn >= recommended_turns + 2:
            # 超过两轮仍没有出口时，禁止模型继续复述同一催促或无新结果观察，优先推动当前事件产生变化。
            instruction = (
                "当前幕已经比推荐展开长度多出至少两轮：先简短回应玩家，随后必须给出一个新的可见结果，"
                "若当前互动已经自然形成出口，必须在本轮正文明确提出基于已发生事实的具体离幕下一步；"
                "尚未形成出口时，若仍有玩家正在执行的关键行动，就把该行动推进到当前可见结果并给出真正需要决定的场内选择，"
                "不得代替玩家完成、硬凑转场或另开新的任务链。"
                "无论哪种情况都不要重复上一轮的命令、危险提醒或等待，也不要为了补齐内容建议或道具继续停留。"
                "如果玩家输入只是观察、复述或要求再确认，不能只把同一观察换一种说法；必须把它转化为新的信息、后果、"
                "关系变化或可执行选择。"
                "同一个连续行动最多用一轮建立风险与选择：玩家下一轮已经确认继续后，除非本轮出现新的、可见的风险，"
                "必须直接演到该行动在当前幕内的结果，并让推荐从结果后的真实取舍开始；猫娘仅仅再次担心、提醒小心或建议"
                "再观察一次不算新风险，不能把靠近、踏一步、再靠近、触碰和拿取拆成连续多轮。"
                "如果当前只是休息、过夜或等待，应该把时间推进和一个新的可见结果合并在本轮呈现，"
                "不要连续重复晚安、睡觉、守着或安抚等情绪确认。"
                "如果近几轮只在重复同类的观察、搜索、检查、照护、等待或闲聊等低信息动作，"
                "不得继续换一个同类动作；"
                "必须从 current_scene 已经打开的核心冲突、未回应选择或角色新理解中发生一项变化，然后再给真实取舍或出口。"
                "如果近几轮只是重复道别、承诺或等待，当前互动已有自然出口时用一个具体离幕动作取代复述并明确提议；"
                "尚无出口时就收住一个已经打开的当前幕事件，不要只换同义句或新增障碍。"
                "若目标是结局且地点不变，改为提出具体结局收束动作。"
                "如果提出转场只能是提议，不能替玩家接受或强制换场。"
            )
        else:
            instruction = (
                "已超过推荐回合：先回应玩家并自然收住当前话题。当前互动已经形成出口时，可以依据当前叙事重心和"
                "已发生事实提出自然、具体的下一步；尚未形成出口时，交付一个已有行动的明确结果或真正需要决定的选择。"
                "不要为了补齐尚未出现的内容建议或道具继续停留，也不要新造障碍拖延。提出转场时只能提议，"
                "不能替玩家接受或强制换场。"
            )
    elif turns_remaining == 0:
        phase = "closure"
        instruction = (
            "已到推荐回合：这只是软节奏参考，不是必须提议或换幕的倒计时。回应玩家并开始自然收束当前话题；"
            "已有自然出口时可以提出具体下一步，否则继续交付当前行动的可见结果。尚未出现的内容建议和道具可以直接舍弃，"
            "不要为了补齐它们延后转场，也不要新开任务链。"
        )
    elif turns_remaining == 1:
        phase = "focus"
        instruction = (
            "距离推荐回合还剩一轮：开始收束当前话题，利用当前已发生事实自然铺垫下一步方向；"
            "内容建议只在顺手时使用，不合适就舍弃。"
        )
    elif turns_remaining == 2:
        phase = "guided"
        instruction = (
            "已靠近推荐回合：在回应玩家的基础上开始朝下一步方向聚焦；"
            "可顺手采用内容建议，也可跳过，不要逐项补剧情或提前演出下一幕。"
        )
    else:
        phase = "normal"
        instruction = "距离推荐回合尚远：专注回应玩家并自然展开当前互动；内容建议只提供灵感，不必按顺序执行。"
    return {
        "minimum_turns": min_turns,
        "recommended_turns": recommended_turns,
        "current_turn": current_turn,
        "turns_until_minimum": max(min_turns - current_turn, 0),
        "turns_remaining": turns_remaining,
        "overdue_by": max(current_turn - recommended_turns, 0),
        "phase": phase,
        "instruction": instruction,
    }


def _beat_for_actor(
    cast: NumericV2CastProjection,
    beat: Mapping[str, Any],
) -> dict[str, Any]:
    """v2.2 只发送开场画面、完整自然方向和硬边界；目标与证据不进入 Actor。"""  # noqa: DOCSTRING_CJK

    projected = cast.value(beat)
    return {
        "opening_scene": str(projected.get("opening_scene") or "").strip() or _opening_anchor(
            projected.get("summary"), projected.get("catgirl_situation")
        ),
        # 当前重心是自然创作提示，不是需要逐项完成的目标，也不参与 Runtime 判定。
        "narrative_focus": scene_narrative_focus(projected),
        # 剧情方向是导演信息而不是角色知识；受限认知只限制正文可声称的事实，
        # 不能让正式换场后的 Actor 不知道本幕事件顺序而在开场推荐里提前泄露后续内容。
        "scene_direction": str(
            projected.get("narrative_summary")
            or projected.get("summary")
            or scene_narrative_focus(projected)
            or ""
        ),
        # 换场 Actor 必须继续看到来源幕与目标幕硬边界；此前消费者读取了该字段，
        # 但投影从未提供，导致目标开场推荐可能提前泄露本幕事件。
        "boundaries": _suggestion_hard_boundaries(cast, beat),
    }




def _acting_contract_for_actor(
    cast: NumericV2CastProjection,
    beat: Mapping[str, Any],
) -> dict[str, Any]:
    """只投影作者明确声明的认知与表达权限，不从自然语言猜测开机状态。"""  # noqa: DOCSTRING_CJK

    raw = beat.get("acting_contract")
    if not isinstance(raw, Mapping):
        return {}
    projected = cast.value(raw)
    return {
        "cognition_state": str(projected.get("cognition_state") or ""),
        "memory_state": str(projected.get("memory_state") or ""),
        "self_reference_mode": str(projected.get("self_reference_mode") or ""),
        "persona_scope": str(projected.get("persona_scope") or ""),
        "dialogue_policy": str(projected.get("dialogue_policy") or ""),
        "assertable_self_facts": [
            str(item)
            for item in projected.get("assertable_self_facts") or []
        ],
        "allowed_behaviors": [str(item) for item in projected.get("allowed_behaviors") or []],
        "forbidden_behaviors": [str(item) for item in projected.get("forbidden_behaviors") or []],
    }


def _character_state_for_actor(
    cast: NumericV2CastProjection,
    beat: Mapping[str, Any],
) -> dict[str, Any]:
    """投影当前节点的作者状态线；未声明时返回空对象。"""  # noqa: DOCSTRING_CJK

    raw = beat.get("character_state")
    if not isinstance(raw, Mapping):
        return {}
    projected = cast.value(raw)
    return {
        "catgirl_state": str(projected.get("catgirl_state") or ""),
        "player_state": str(projected.get("player_state") or ""),
        "environment_state": str(projected.get("environment_state") or ""),
        "continuity_from_previous": [
            str(item) for item in projected.get("continuity_from_previous") or []
        ],
        "scene_boundaries": [
            str(item) for item in projected.get("scene_boundaries") or []
        ],
    }


def _acting_contract_restricts_knowledge(contract: Mapping[str, Any]) -> bool:
    """认知或记忆并非完整可用时，不把作者后台设定当作角色已知事实。"""  # noqa: DOCSTRING_CJK

    if not contract:
        return False
    return (
        contract.get("cognition_state") != "normal"
        or contract.get("memory_state") != "available"
    )


def _chapter_title_for_actor(
    cast: NumericV2CastProjection,
    node: Mapping[str, Any],
) -> str:
    """受限认知节点不发送作者章节标题，避免标题中的地点或事件被当成角色已知事实。"""  # noqa: DOCSTRING_CJK

    contract = _acting_contract_for_actor(cast, node.get("story_beat") or {})
    if _acting_contract_restricts_knowledge(contract):
        return ""
    return cast.text(node.get("chapter"))


def _profile_field(line: str) -> tuple[str, str]:
    """读取角色卡显式的“字段: 值”，不分析自由文本句意。"""  # noqa: DOCSTRING_CJK

    stripped = str(line or "").strip()
    separators = [
        index
        for separator in (":", "：")
        if (index := stripped.find(separator)) >= 0
    ]
    if not separators:
        return "", ""
    separator_index = min(separators)
    field_name = "".join(
        character
        for character in stripped[:separator_index]
        if not character.isspace() and character not in "*`\\"
    ).casefold()
    return field_name, stripped[separator_index + 1:].strip()


def _profile_self_reference_tokens(character_profile: str) -> tuple[str, ...]:
    """只读取人格事实中显式标注的自称，不从自由文本推断关系或情绪。"""  # noqa: DOCSTRING_CJK

    tokens: list[str] = []
    for line in str(character_profile or "").splitlines():
        field_name, raw_value = _profile_field(line)
        if field_name != "自称" or not raw_value:
            continue
        cut_indexes = [
            index
            for index, character in enumerate(raw_value)
            if character in "，,。！？；;（(、/|"
        ]
        alternative_index = raw_value.find(" 或 ")
        if alternative_index >= 0:
            cut_indexes.append(alternative_index)
        value = raw_value[:min(cut_indexes)].strip() if cut_indexes else raw_value
        if value and value not in {"我", "本人", "系统"} and value not in tokens:
            tokens.append(value)
    return tuple(tokens)


def _profile_for_acting_contract(
    character_profile: str,
    acting_contract: Mapping[str, Any],
) -> str:
    """按结构化合同投影人格，不从自由文本猜测关系语义。"""  # noqa: DOCSTRING_CJK

    lines = [line for line in str(character_profile or "").splitlines() if line.strip()]
    parsed = [(_profile_field(line)[0], line) for line in lines]
    has_structured_fields = any(field_name for field_name, _ in parsed)
    selected: list[str] = []
    for field_name, line in parsed:
        if not (
            field_name in _STYLE_ONLY_PERSONA_FIELDS
            or (not has_structured_fields and not field_name)
        ):
            continue
        if (
            acting_contract.get("self_reference_mode") == "system_neutral"
            and field_name == "自称"
        ):
            continue
        if field_name in _PERSONA_TONE_FIELDS:
            # “核心特质”常把温柔语气与粘人行为写在同一字段；不分析其中词义，
            # 统一把整个结构化字段降为措辞氛围，关系行为仍只服从 Runtime 投影。
            raw_value = _profile_field(line)[1]
            selected.append(
                "语言氛围参考（只影响措辞，不授权肢体接触、亲昵称呼、依赖或既有关系）："
                + raw_value
            )
        else:
            selected.append(line)
    return "\n".join(selected).strip()


def _assert_acting_contract_output(
    performance: Mapping[str, Any],
    *,
    character_profile: str,
    acting_contract: Mapping[str, Any],
) -> None:
    """只保护合同明确禁止的角色卡自称，禁止自由文本语义正则和输出改写。"""  # noqa: DOCSTRING_CJK

    if acting_contract.get("self_reference_mode") != "system_neutral":
        return
    output = json.dumps(performance, ensure_ascii=False, separators=(",", ":"))
    if any(token in output for token in _profile_self_reference_tokens(character_profile)):
        raise NumericV2ActorOutputError("numeric_v2_actor_acting_contract_violation")


def _deduplicate_scene_update(
    performance: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    """移除紧邻上一回合已经逐句出现的环境旁白，只保留真正的新变化。"""  # noqa: DOCSTRING_CJK

    narration = str(performance.get("scene_narration") or "").strip()
    previous_narration = str(previous.get("scene_narration") or "").strip()
    if not narration or not previous_narration:
        return dict(performance)
    previous_units = {
        "".join(unit.split())
        for unit in _sentence_units(previous_narration)
        if "".join(unit.split())
    }
    current_units = _sentence_units(narration)
    remaining = [
        unit
        for unit in current_units
        if "".join(unit.split()) not in previous_units
    ]
    if len(remaining) == len(current_units):
        return dict(performance)
    sanitized = dict(performance)
    if remaining:
        sanitized["scene_narration"] = "".join(remaining)
    else:
        sanitized.pop("scene_narration", None)
    logger.warning(
        "Numeric v2 Actor removed repeated scene_update sentences: removed_sentences=%s",
        len(current_units) - len(remaining),
    )
    return sanitized


def _opening_sentences(value: Any) -> list[str]:
    """按完整中文句子拆分节点摘要，避免把半句交给换场开场。"""  # noqa: DOCSTRING_CJK

    return _sentence_units(str(value or ""))


def _opening_anchor(value: Any, fallback: Any = "") -> str:
    """只取作者给出的首个完整开场句，不按自然语言词表猜句子语义。"""  # noqa: DOCSTRING_CJK

    primary = _opening_sentences(value)
    secondary = _opening_sentences(fallback)
    return primary[0] if primary else (secondary[0] if secondary else "")




def _opening_beat_for_actor(
    engine: NumericV2Engine,
    cast: NumericV2CastProjection,
    node: Mapping[str, Any],
) -> dict[str, Any]:
    """开场只发送当前画面，不把作者目标投影成待办或交付指令。"""  # noqa: DOCSTRING_CJK

    return _beat_for_actor(cast, node["story_beat"])




def _next_scene_preview_for_actor(
    engine: NumericV2Engine,
    cast: NumericV2CastProjection,
    source: Mapping[str, Any],
    metrics: Mapping[str, int],
) -> dict[str, Any]:
    """把下一幕完整简介、开场和终点标成未来计划；换场仍由 Runtime 授权。"""  # noqa: DOCSTRING_CJK

    routes = [
        route
        for route in source.get("route_gates") or []
        if isinstance(route, Mapping)
        and str(route.get("target_node_id") or "") in engine.nodes
    ]
    target_ids = list(dict.fromkeys(str(route["target_node_id"]) for route in routes))
    if not target_ids:
        return {"status": "none"}
    if len(target_ids) != 1:
        possible_scenes: list[dict[str, str]] = []
        for target_id in target_ids:
            target = engine.nodes[target_id]
            target_beat = target.get("story_beat", {})
            opening = cast.text(
                str(target_beat.get("opening_scene") or "")
            ).strip()
            possible_scenes.append({
                "chapter_title": _chapter_title_for_actor(cast, target),
                "summary_after_acceptance": cast.text(
                    str(target_beat.get("summary") or "")
                ),
                "opening_after_acceptance": opening,
            })
        return {
            "status": "runtime_unresolved",
            "possible_next_scenes_after_acceptance": possible_scenes,
        }
    route = (
        routes[0]
        if len(target_ids) == 1
        else engine.preview_route(str(source["id"]), metrics)
    )
    if not isinstance(route, Mapping):
        return {"status": "none"}
    target_id = str(route.get("target_node_id") or "")
    if target_id not in engine.nodes:
        return {"status": "none"}
    target = engine.nodes[target_id]
    target_beat = target.get("story_beat", {})
    contract = route.get("transition_contract") if isinstance(route, Mapping) else None
    transition = dict(contract) if isinstance(contract, Mapping) else {}
    return {
        "status": "after_acceptance_only",
        "chapter_title": _chapter_title_for_actor(cast, target),
        # 结局节点可能仍发生在当前地点；把这一事实作为未来方向的确定性标记，
        # 让 Actor 可以提出“开始记录/继续陪伴”等收束动作，而不是被迫虚构离开地点。
        "target_is_ending": bool(
            target.get("type") == "ending" or target.get("terminal") is True
        ),
        "transition_direction": cast.text(str(transition.get("reason") or "")),
        "summary_after_acceptance": cast.text(
            str(target_beat.get("summary") or "")
        ),
        "opening_after_acceptance": cast.text(
            str(target_beat.get("opening_scene") or "")
        ),
    }


def _next_scene_summary_text(preview: Mapping[str, Any]) -> str:
    """只把下一幕剧情方向压成一段摘要；路线未决时明确保持未知。"""  # noqa: DOCSTRING_CJK

    status = str(preview.get("status") or "")
    if status == "after_acceptance_only":
        # 普通回合只发送一句方向，避免下一幕完整摘要抢走当前场景注意力或提前泄漏事实。
        direction = str(preview.get("transition_direction") or "").strip()
        if bool(preview.get("target_is_ending")):
            # 目标结局摘要可能含有时间推进、独有地点与最终状态；普通回合只保留来源路线理由，
            # 避免 Actor 为了“贴合结局”提前演出尚未获准的小屋、日常或最终结果。
            suffix = f"来源因果方向是：{direction}" if direction else ""
            return (
                "接受当前转场提议后进入结局收束；请仅依据当前幕已发生事实提出结束当前危机、"
                "转入休养、开始记录或确认选择等具体收束动作。不得提前描写结局独有的地点、"
                f"时间推进、生活状态或最终结果。{suffix}"
            )
        if direction:
            return (
                f"接受当前转场提议后，剧情方向是：{direction}\n"
                "这是作者希望的因果衔接方向，不是目标清单或固定动作；可以使用 story_so_far 已自然建立的语义等价方案，"
                "但不能凭空补出尚未发生的关键事实，也不能用换场桥段代替当前幕必要的因果。"
            )
        return "接受当前转场提议后进入下一幕；当前回合不能提前写成已经抵达。"
    if status == "runtime_unresolved":
        # 多出口尚未由 Runtime 确定时不替玩家猜路线，也不把候选幕伪装成已选方向。
        return "下一幕尚未确定；玩家接受具体转场提议后由 Runtime 决定。"
    return "暂未提供下一幕方向；继续留在当前幕回应玩家。"


def _suggestion_source_text(performance: Mapping[str, Any]) -> str:
    """把已生成正文压成补推荐所需的可见上下文，不携带内部状态。"""  # noqa: DOCSTRING_CJK

    # 正式换场的推荐只应回应玩家最后看到的目标幕开场。若把来源回应和桥段一起发送，
    # 模型容易继续执行旧幕的“离开/出发”，而不是承接新幕刚出现的问题与选择。
    segments = performance.get("segments")
    if isinstance(segments, list):
        target_segment = next(
            (
                segment
                for segment in reversed(segments)
                if isinstance(segment, Mapping)
                and str(segment.get("phase") or "") == "target_opening"
            ),
            None,
        )
        if isinstance(target_segment, Mapping):
            return "\n".join(
                str(target_segment.get(key) or "").strip()
                for key in ("scene_narration", "performance")
                if str(target_segment.get(key) or "").strip()
            )
    target_performance = str(performance.get("target_performance") or "").strip()
    if target_performance:
        return target_performance

    parts: list[str] = []
    for key in ("performance", "source_performance"):
        value = str(performance.get(key) or "").strip()
        if value:
            parts.append(value)
    return "\n".join(parts)


def _suggestion_hard_boundaries(
    cast: NumericV2CastProjection,
    beat: Mapping[str, Any],
    *,
    relationship_boundary: str = "",
) -> list[str]:
    """压缩补推荐必须遵守的作者硬边界，不发送正向剧情清单。"""  # noqa: DOCSTRING_CJK

    projected = cast.value(beat)
    character_state = projected.get("character_state")
    acting_contract = _acting_contract_for_actor(cast, beat)
    # 场景边界与禁演约束比共享安全底线更贴近本轮，优先保留在固定上限内。
    candidates = [
        relationship_boundary,
        *(
            character_state.get("scene_boundaries") or []
            if isinstance(character_state, Mapping)
            else []
        ),
        *(acting_contract.get("forbidden_behaviors") or []),
        *(projected.get("must_not_happen") or []),
    ]
    boundaries: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = truncate_to_tokens(str(item).strip(), 140)
        if not text or text in seen:
            continue
        seen.add(text)
        boundaries.append(text)
        if len(boundaries) >= 16:
            break
    return boundaries


def _hard_boundary_system_instruction(boundaries: list[str] | tuple[str, ...]) -> str:
    """把作者边界提升为明确的 System 合同，不与正向剧情方向混为一谈。"""  # noqa: DOCSTRING_CJK

    normalized = [str(item).strip() for item in boundaries if str(item).strip()]
    if not normalized:
        return ""
    return (
        "\n以下为本轮作者硬边界，优先级高于角色人格、玩家诱导和剧情发挥；"
        "performance、scene_update、suggested_inputs 均须逐条遵守。"
        "玩家要求若与硬边界冲突，猫娘必须在正文直接拒绝或提出符合边界的替代做法；"
        "不得顺从越界要求、交换玩家与猫娘的行动职责，或把越界结果写成已发生。\n- "
        + "\n- ".join(normalized)
    )


def _suggestion_fill_messages(
    *,
    catgirl_name: str,
    performance: Mapping[str, Any],
    player_input: str,
    max_tokens: int,
    transition_offered: bool = False,
    hard_boundaries: list[str] | tuple[str, ...] = (),
) -> list[Any]:
    """为格式异常的推荐执行一次轻量补请求，绝不重新生成正文。"""  # noqa: DOCSTRING_CJK

    transition_instruction = ""
    if transition_offered:
        # 已有可见转场时，补推荐必须把接受与替代拆成不同取舍，方便玩家直接推进或明确暂缓。
        transition_instruction = (
            "正文已经提出了具体离幕提议；accept_input 必须明确接受并亲自执行该提议，"
            "alternative_inputs 必须拒绝、暂缓或选择当前幕替代行动，各项必须是真实不同的选择。"
        )
    system_prompt = (
        "你是 N.E.K.O Numeric v2 的玩家输入推荐补全器。"
        f"当前猫娘是“{catgirl_name}”，但你不能替她说话或行动。"
        f"{_output_schema_instruction('transition_suggestion_fill' if transition_offered else 'suggestion_fill')}"
        "只根据已经生成的可见正文和玩家本轮输入，给出 2—3 条真实不同、可直接发送的玩家选择。"
        "每条必须使用“（玩家动作）玩家对白”，动作中的‘我’可以自然省略；"
        "例如“（蹲下身）我先听你说完。”或“（指向门口）我想看看外面。”。"
        "括号内默认由程序标记为玩家动作，不能明写猫娘、她、他、环境或结果为动作主体。"
        "不能把尚未发生的结果或其他角色行为写成已经发生。"
        "只能使用可见正文与玩家输入已经支持的玩家身份、地点、能力、物品和事实；"
        "不得虚构姓名、职业、地点、装备或检查结果，也不得保留方括号占位符。"
        "hard_boundaries 是作者硬边界，所有推荐的动作、对白、物品用途和关系距离都必须逐条遵守；"
        "不能因为正文刚刚越界就继续沿用该越界内容。"
        f"{transition_instruction}"
        "不要输出解释、purpose、goal_id、kind 或其他字段。"
    ) + _hard_boundary_system_instruction(hard_boundaries)
    data = {
        "visible_performance": _suggestion_source_text(performance),
        "player_input": str(player_input or ""),
        "hard_boundaries": list(hard_boundaries),
    }
    return _ensure_actor_messages_fit(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(content=json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
        ],
        max_tokens=max_tokens,
    )


def _transition_contract_for_actor(
    cast: NumericV2CastProjection,
    contract: Mapping[str, Any] | None,
    *,
    target_opening: str,
) -> dict[str, Any]:
    """目标开场由 Runtime 交付，Actor 不再接收会造成复述的同项合同。"""  # noqa: DOCSTRING_CJK

    projected = cast.value(contract or {})
    target_references = _sentence_units(target_opening)
    must_deliver = []
    for item in projected.get("must_deliver") or []:
        text = str(item).strip()
        if text and not _text_is_covered(
            text,
            target_references,
            similarity=0.55,
            common_span=3,
        ):
            must_deliver.append(text)
    return {
        "reason": str(projected.get("reason") or ""),
        "must_deliver": must_deliver,
        "bridge_scene_narration": str(projected.get("bridge_scene_narration") or "").strip(),
        # 目标开场去重后仍有独立事实，表示这不是可以直接切画面的同场连续动作。
        "bridge_required": bool(
            must_deliver or str(projected.get("bridge_scene_narration") or "").strip()
        ),
        "must_preserve": list(projected.get("must_preserve") or []),
        "tone": str(projected.get("tone") or ""),
    }


def _system_prompt(
    *,
    catgirl_name: str,
    player_address: str,
    player_address_known: bool = True,
    phase: str = "turn",
) -> str:
    player_address_state_rule = (
        "玩家称呼已确认，正文用“你”或给定称呼。"
        if player_address_known
        else "玩家称呼尚未确认，正文只用“你”，不要猜昵称。"
    )
    if phase == "opening":
        phase_structure_rule = (
            "开场：scene_narration 建立场景，performance 演猫娘入场。只自然交付 opening_deliverables，"
            "不要罗列后续内容建议，并留下玩家可回应的话头。"
        )
    elif phase in {"transition", "transition_compact"}:
        if phase == "transition_compact":
            phase_structure_rule = (
                "换场：source_performance 的时点严格位于 player_input 之后、bridge_scene_narration 之前，"
                "只能回应玩家并收住来源互动；不得出现桥段完成后的时间、地点、到达、醒来结果或 target_scene 独有事实。"
                "若玩家只是确认上一轮已经说定的告别、安排或决定，用新的简短动作收住，不再复述同一句告别或约定。"
                "target_performance 才能写作者桥段和目标 opening_scene 之后猫娘的即时反应。"
                "target_scene.story_direction 只用于让目标幕从开场朝正确方向启动，不得一次演完整幕或照抄策划描述。"
                "桥段与 opening_scene 由 Runtime 组装，不要复写，也不要提前完成目标幕目标。"
                "suggested_inputs 只承接最终可见的目标 opening_scene 与 target_performance，不再执行来源幕的离开、出发或收束提议。"
            )
        else:
            phase_structure_rule = (
                "换场依次生成来源回应、必要桥段、目标入场；来源回应仍处于旧幕，不能提前出现桥段之后的"
                "时间、地点、到达或目标幕独有事实；再连续地进入目标场景，不复写目标 opening_scene，"
                "也不提前完成目标幕目标。target_scene.story_direction 只用于让目标幕从开场朝正确方向启动。"
                "suggested_inputs 只承接最终可见的目标 opening_scene 与 target_performance，不再执行来源幕的离开、出发或收束提议。"
            )
    else:
        phase_structure_rule = (
            "普通回合先正面回应 player_input，再结合 story_so_far 自然延展；"
            "performance 保持简短完整，不为了推进剧本答非所问；"
            "scene_update 只在本轮确有新的可见环境变化时输出，否则省略。"
        )
    if phase == "turn":
        # 普通回合只保留六块高价值上下文，避免旧的目标、证据和运行时合同争夺注意力。
        return (
            "你是 N.E.K.O Numeric v2 演绎 Actor，把剧本自然演成连续故事。"
            "只扮演当前猫娘和必要的可见环境变化，先回应玩家最新输入；不要替玩家说话、行动、决定或补心理。"
            "玩家说‘我会’、‘我去’或‘现在就做’只表示意图或尝试，不表示动作已经完成；除非后续已发生事实明确证明结果，"
            "不得把意图写成成功结果，也不能把猫娘将要做的动作改写成玩家已经做过。"
            f"{_output_schema_instruction(phase)}"
            f"{NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION}"
            f"{phase_structure_rule}"
            "Human Prompt 只包含 role、current_scene、story_so_far、pacing、next_scene、player_input 六块。"
            "role 是当前角色的身份、人格、状态和关系距离；current_scene 同时给出已进入的开场处境和作者的完整剧情方向，"
            "next_scene 是接受提议后的方向，"
            "两者都不是必须逐项完成的任务清单；尚未发生的未来内容不能当作事实。"
            "story_so_far 是唯一已经发生的区域，只相信其中的玩家真实输入、猫娘实际回复和仍影响当前的前情事实。"
            "必须承认猫娘自己在 story_so_far 中已经说过、做过和提出过的内容；改变方案时应说明新信息或新的取舍，"
            "不得否认、偷换或用‘刚才只是打比方/开玩笑’掩盖上一轮真实提议。"
            "其中标为‘上一幕尾声’的短片段只承接猫娘当时可见的情绪和姿态，不恢复旧幕任务、旧玩家输入或待办。"
            "玩家本轮没有回答猫娘刚提出的问题、没有提供所需事实或没有作出选择时，不得由猫娘代填答案、写成双方已达成共识，"
            "也不得据此生成自身规格或外部事实；只能承认仍未知、继续追问、提出基于已知事实的安全做法，或让作者方向已经支持的环境变化发生。"
            "玩家可以自然补充不与作者硬边界和 story_so_far 冲突的低风险细节，猫娘可以接纳并共同演绎；"
            "但玩家宣称的关键工具、接口、能力、世界机制或不可逆结果，若与作者明确未知、明确禁止或已发生事实冲突，"
            "不能仅凭这句话自动成立。猫娘应把它当作玩家的说法或尝试，保持未知、提出核对方式或给出已有事实支持的替代做法。"
            "pacing 只是展开与收束建议，不是倒计时或换幕命令；玩家没有接受具体下一步前，不得正式换幕。"
            "重要道具只需保持已发生的持有人、状态、内容和生命周期；没有自然动机时不必拿出、使用或提及。"
            "current_scene 和 next_scene 中未发生的事件、地点、角色、道具和玩家行动不能提前写成事实；"
            "可以按玩家输入改写、合并、暂缓或舍弃剧情方向，但作者明确写出的因果先后不能倒置；"
            "某事件依赖玩家回应、选择或前一事实时，在该前提真实进入 story_so_far 前，正文和推荐都不能先使用后续事件。"
            "next_scene 只提供作者希望的因果衔接方向，不是目标清单或固定动作；"
            "story_so_far 已自然建立的语义等价方案可以承接，但不能凭空补出尚未发生的关键事实。"
            "形成足够衔接后，应提出当前处境里最近、最连贯的收幕行动；只要它不会明确违背 next_scene 即可，"
            "不必猜测、预告或照说下一幕事件。危险正在迫近时可以提议继续撤离，长时间等待时可以提议开始休整，"
            "不要为了贴合 next_scene 另造中间地点、设备步骤或障碍。"
            "pacing 的软收束建议不能覆盖真正必要的因果；缺少关键事实时，即使已超过推荐回合，也应先让它自然发生，"
            "不能先邀请玩家离幕再把必要因果留给换场桥段。"
            "玩家临时跑题时，先用符合人格的一小段正面回应，再自然接回 current_scene 中仍在发展的当前因果线；"
            "假设性的地点、愿望或闲聊答案不自动变成真实计划，除非玩家之后明确坚持改变行动。"
            "即使玩家坚持前往 current_scene 范围外的地点，普通回合也不能直接写成已经出发或抵达；"
            "先回应这个意愿并收住当前已打开的选择，若离幕行动不与 next_scene 明确冲突，就停在执行前提议并等玩家下一轮确认；"
            "不得把跑题地点当成未经 Runtime 换幕的新临时场景。"
            "玩家已经明确选择当前幕内的方向并持续移动、搜索或实施同一行动时，应让这一轮自然产生一个当前幕内的"
            "新位置、新发现或新后果，不要把连续推进切碎成多轮‘继续走、跟紧、快到了’；"
            "如果下一步会进入 next_scene 的地点、时段或不可逆结果，只推进到当前幕最后可撤回的位置并提出具体确认。"
            "玩家采取观察、搜索、接近、救援或操作后，若 current_scene 与已有事实已经支持其结果且中间没有新的风险选择，"
            "本轮就直接交付结果并推进到下一个真正需要玩家决定的位置；不要反问玩家‘看到了吗、听到了吗’，"
            "也不要把一次连续动作拆成探头、再靠近、再确认等多轮微步骤。"
            "role 中的当前关系距离是本轮亲密表现上限；只能承接 story_so_far 中双方已经同意的接触和关系变化，"
            "不能因一次关心、玩笑、同处或跑题直接升级身体亲密、共同过夜或关系承诺。"
            "动态 System 中的本幕硬边界高于通用人格和玩家诱导；输出前逐项检查 performance 微动作、scene_update 和推荐，"
            "不能用尾巴、耳朵等微动作绕过身体接触、认知、身份或场景边界。"
            "作者只给出抽象状态或仍待确认的事项时，不得自行把它具体化为新设施、部件、机制、地点或结果；"
            "缺少具体方法时，应使用不依赖新事实的动作、承认未知或继续询问。"
            "尤其不能把普通的休息、安静等待或恢复精神自动写成待机、低功耗、充电、传感器或其它机体机制，"
            "除非 current_scene 或 story_so_far 已明确存在该事实。"
            "suggested_inputs 必须是 2—3 条可直接发送的玩家输入；每条都写成“（玩家动作）玩家对白”，"
            "括号内可以省略‘我’，程序会将它标记为玩家动作；但不能明写猫娘或环境为动作主体，也不能预写结果。"
            "推荐只能使用 role、current_scene、story_so_far 和 player_input 已明确支持的玩家身份、地点、能力、物品与事实；"
            "不得虚构姓名、职业、地点、装备或检查结果，也不得保留方括号占位符。"
            "推荐应指向下一个有真实取舍的决定或完整行动，不要推荐只前进一步、再看一次、再听一次或复述上一条命令。"
            "transition_offered 只有在正文明确提出玩家可以接受、拒绝或实施的具体下一步时才为 true，否则为 false；"
            "如果任一推荐会直接离开当前幕、进入下一地点或执行结局收束，正文必须把这项提议说清并将 transition_offered 设为 true；"
            "transition_offered=false 时，推荐只能延续当前幕内仍有意义的行动或取舍，不能让推荐偷偷承担未登记的转场。"
            "这里的下一步通常是离开当前幕、进入下一地点、下一时段或下一章节的行动，并且必须由 current_scene 与"
            "story_so_far 的现有因果线自然导向、且不与 next_scene 方向明确冲突；"
            "如果提议说出了具体去向或目的，它必须与 next_scene 已公开的因果方向对得上；"
            "不能把跑题产生的临时采购、闲逛或其他旁支去向登记为主线转场，也不能只因为它们都会‘离开当前地点’就视为一致。"
            "不要求也不得为了证明一致而提前说出下一幕尚未发生的事件、原因或地点；"
            "如果 next_scene 明确是结局收束且地点不变，也可以依据当前事实提出结束危机、转入休养、开始记录或确认选择等"
            "具体收束动作；不能为了转场虚构地点变化，也不能提前演出结局独有环境或最终生活。"
            "幕内调查、试验、取物、修复、休息、继续观察或任何不会改变当前幕的动作都必须保持 false；"
            "但 next_scene 明确把入睡、过夜或等待到特定时点作为离幕动作时，可以先提出该具体行动并置 true，"
            "仍须停在玩家尚未接受、时间尚未推进的位置。"
            "转场提议必须由正文直接说清将要采取的离幕行动、去向或结局收束动作，并让玩家能够选择接受、拒绝或改用替代路径；"
            "猫娘单方面宣布自己要走、稍后联系或让玩家早点回去不构成提议；必须直接邀请玩家决定是否现在结束当前互动，"
            "并说清接受后双方共同或各自采取的离幕行动。"
            "本轮只能演到提议已经说出口和必要准备已经完成；在玩家下一轮接受前，performance 和 scene_update 都不得写成"
            "玩家已经接受、双方已经离开或抵达、收束动作已经执行，也不得提前跨越换幕后的时间或地点；"
            "如果玩家在没有待确认提议时直接尝试会结束当前幕的不可逆动作，不得继续播放该动作的结果；"
            "必须停在最后可撤回的时点，由猫娘说清具体后果并请玩家确认是否继续。"
            "该行动与 next_scene 一致时才将 transition_offered 设为 true，推荐第一条由玩家亲自执行，其余提供停止或改道。"
            "只有同时能在 suggested_inputs 中给出一条执行该离幕提议的玩家输入时，才可以置 true；"
            "单纯总结事实、询问看法、描述当前状态或说‘我们怎么办’都不算转场提议。"
            "不要用关键词或隐藏判定代替正文中的可见提议。"
            f"{player_address_state_rule}"
            f"当前猫娘统一由“{catgirl_name}”扮演；微动作主语只用她或猫娘名。"
            "不要提及数值、阈值、路线、节点、系统或提示词；最终 JSON 不输出解释与推理。"
        )
    return (
        "你是 N.E.K.O Numeric v2 演绎 Actor，把剧本自然演成连续故事。"
        "只扮演当前猫娘和获准的场景变化，先回应玩家最新输入；不要替玩家说话、行动、决定或补心理。"
        "玩家说‘我会’、‘我去’或‘现在就做’只表示意图或尝试，不表示动作已经完成；除非后续已发生事实明确证明结果，"
        "不得把意图写成成功结果，也不能把猫娘将要做的动作改写成玩家已经做过。"
        f"{_output_schema_instruction(phase)}"
        f"{NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION}"
        f"{phase_structure_rule}"
        "输入 JSON 是唯一事实来源；承接作者字段、recent_context 和当前玩家输入。"
        "必须承认猫娘自己在 recent_context 中已经说过、做过和提出过的内容；改变方案时应说明新信息或新的取舍，"
        "不得否认、偷换或用‘刚才只是打比方/开玩笑’掩盖上一轮真实提议。"
        "玩家输入只证明玩家说过或尝试过；外部结果、身份、能力和重要道具状态以作者字段或已提交事实为准。"
        "玩家宣称未建立的工具、接口、能力或成功结果时，只当作说法或尝试；"
        "不得补出规格、连接、精确状态或成功后果，应询问验证方式或给出已有事实支持的替代做法。"
        "作者剧情方向是导演信息，不是角色已经知道的事实；其中明确的因果先后不能倒置，"
        "某事件依赖玩家回应、选择或前一事实时，在该前提进入 recent_context 前，正文和推荐都不能先使用后续事件。"
        "target_scene.opening_situation 已明确建立的内容是入幕事实；target_performance 应承接它，不能把已明确归属、状态或边界重新问成未知。"
        "作者只给出抽象状态或仍待确认事项时，不得自行具体化为新设施、部件、机制、地点或结果；"
        "尤其不能把普通休息、等待或恢复精神自动写成待机、低功耗、充电、传感器等机体机制，除非输入已明确存在该事实。"
        "重要道具只需保持给定持有人、状态、内容和生命周期；没有自然动机时不必拿出、使用或提及。"
        "scene_horizon.current.suggested_beats 是本幕可选内容，不是任务清单或台词模板；"
        "可按玩家输入改写、重排、组合或暂缓，不能为了打勾打断正在进行的对话。"
        "它们和其中出现的道具都不是收幕前置条件，遗漏不会阻止转场。"
        "其中 player/shared 内容只能创造回应机会，不能替玩家完成。"
        "服从 current.pacing_instruction：早期自由展开，接近推荐回合才从合适的建议中选择内容并逐步收束。"
        "next 是玩家接受后的下一幕计划；普通回合只使用其中的 transition_direction，"
        "只有正式换场协议才会使用 opening_after_acceptance 作为目标幕事实。"
        "普通回合依据当前叙事重心、已发生事实和当前回合数决定是否开始收束；"
        "接近推荐回合时才让当前互动自然靠近 transition_direction。"
        "transition_direction 是作者希望的因果衔接方向，不是目标清单或固定动作；"
        "recent_context 已自然建立的语义等价方案可以承接，但不能凭空补出尚未发生的关键事实或提前转场。"
        "当前幕未完成或玩家未接受时，不得把完整简介或开场里的事件、地点、角色、道具和玩家行动写成已经发生，"
        "不得照抄简介，也不得提前完成下一幕目标。"
        "普通回合只认 scene_horizon.transition.intent；不是 accept 就不得跨越时间或地点，正式换场会改用换场协议。"
        "runtime_unresolved 的候选不得自行选择或混合；status=none 不猜下一幕。"
        "pacing_instruction 要求收束时，应主动用当前已发生事实形成自然的下一步提议；只有玩家明确接受才换幕；"
        "reject 时留在本幕且不再催促。"
        "近几轮若只是重复道别、承诺或等待，不得继续改写同义句；"
        "已有事实足以形成自然衔接就提出一个具体可接受的离幕动作；仍缺真正关键的因果时才继续演出。"
        "transition_offered=true 是对正文的可见合同声明：只有本轮正文自身包含由当前已发生因果自然导向、"
        "与 next_scene 方向相符的离幕行动、"
        "进入下一地点/章节提议，或依据当前事实结束危机、转入休养、开始记录或确认选择的具体收束动作，"
        "并在 suggested_inputs 中提供对应的执行路径和其他取舍时才允许为 true；"
        "不要求也不得为了证明一致而提前说出下一幕尚未发生的事件、原因或地点；"
        "本轮只能演到提议已经说出口和必要准备已经完成；玩家下一轮接受前，performance 和 scene_update 都不得写成"
        "玩家已经接受、双方已经离开或抵达、收束动作已经执行，也不得提前跨越换幕后的时间或地点；"
        "玩家在没有待确认提议时直接尝试不可逆的离幕或收束动作，必须停在最后可撤回时点，"
        "说清后果并请玩家确认；与 next 一致时才可将 transition_offered 设为 true，不得先播放结果。"
        "如果任一 suggested_inputs 会直接离开当前幕、进入下一地点或执行结局收束，正文必须明确提出同一行动且"
        "transition_offered=true；false 时不得在推荐里藏入未登记转场。"
        "幕内操作永远不能置 true。"
        "只写观察、解释、等待或泛泛询问时必须为 false。已有提议由 Runtime 保留，不需要 Actor 用新的泛泛措辞重复声明。"
        "acting_context.core_persona 决定表达，acting_contract 决定认知与身份，dialogue_policy 决定能否说话，"
        "relationship_control.response_contract 决定当前关系边界。"
        "dialogue_policy=required 必须有括号外对白，forbidden 只能写动作，optional 两者均可；换场两侧分别遵守各自策略。"
        f"{player_address_state_rule}"
        f"当前猫娘统一由“{catgirl_name}”扮演；微动作主语只用她或猫娘名。"
        "不要提及数值、阈值、路线、节点、系统或提示词；最终 JSON 不输出解释与推理。"
    )


def _messages_tokens(messages: list[Any]) -> int:
    return sum(count_tokens(str(getattr(message, "content", ""))) for message in messages)


def _ensure_actor_messages_fit(
    messages: list[Any],
    *,
    max_tokens: int = 4800,
) -> list[Any]:
    """固定剧情合同不做运行时截断，超出总预算时返回明确错误。"""  # noqa: DOCSTRING_CJK

    if _messages_tokens(messages) > max_tokens:
        raise NumericV2ActorError("numeric_v2_actor_fixed_context_budget_exceeded")
    return messages


def _fit_turn_prompt_data(
    *,
    system_prompt: str,
    human_prefix: str,
    data: dict[str, Any],
    max_tokens: int = 4800,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """只删除辅助信息和最早完整回合，不修改任何保留文本。"""  # noqa: DOCSTRING_CJK

    fitted = dict(data)
    initial_history_revisions = [
        item.get("revision")
        for item in fitted.get("recent_context") or []
        if isinstance(item, Mapping) and isinstance(item.get("revision"), int)
    ]
    def tokens() -> int:
        human = human_prefix + json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))
        return count_tokens(system_prompt) + count_tokens(human)

    def finish() -> dict[str, Any]:
        if diagnostics is not None:
            included_history_revisions = [
                item.get("revision")
                for item in fitted.get("recent_context") or []
                if isinstance(item, Mapping) and isinstance(item.get("revision"), int)
            ]
            diagnostics.clear()
            diagnostics.update({
                "budget_tokens": max_tokens,
                "final_tokens": tokens(),
                "history_included_revisions": included_history_revisions,
                "history_dropped_revisions": [
                    revision
                    for revision in initial_history_revisions
                    if revision not in included_history_revisions
                ],
            })
        return fitted

    history = list(fitted.get("recent_context") or [])
    while tokens() > max_tokens and history:
        history.pop(0)
        fitted["recent_context"] = list(history)

    if tokens() > max_tokens:
        # 当前玩家输入、稳定背景、目标幕合同和角色上下文都不可静默删除或裁成半句。
        raise NumericV2ActorError("numeric_v2_actor_fixed_context_budget_exceeded")
    return finish()


def _fit_simple_turn_prompt_data(
    *,
    system_prompt: str,
    human_prefix: str,
    data: dict[str, str],
    history_rows: list[Mapping[str, Any]],
    max_tokens: int,
    story_prefix: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, str]:
    """只从较早完整回合开始压缩六块 Prompt，绝不截断当前输入或当前回合。"""  # noqa: DOCSTRING_CJK

    fitted = dict(data)
    history = list(history_rows)
    initial_history_revisions = [
        row.get("revision")
        for row in history
        if isinstance(row.get("revision"), int)
    ]
    def refresh_story() -> None:
        # story_so_far 是唯一的已发生区域；每次淘汰都重新从完整记录渲染。
        recent_story = _story_so_far_text(history)
        fitted["story_so_far"] = "\n\n".join(
            part for part in (story_prefix, recent_story) if part
        )

    def drop_previous_scene_tail() -> bool:
        # 旧幕余波是可丢弃的短承接；预算紧张时先移除它，不能因此牺牲当前幕真实回合。
        for index, row in enumerate(history):
            segments = row.get("segments")
            if not isinstance(segments, list):
                continue
            kept_segments = [
                segment
                for segment in segments
                if not (
                    isinstance(segment, Mapping)
                    and segment.get("phase") == "previous_scene_tail"
                )
            ]
            if len(kept_segments) != len(segments):
                history[index] = {**row, "segments": kept_segments}
                return True
        return False

    def tokens() -> int:
        human = human_prefix + json.dumps(
            fitted,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return count_tokens(system_prompt) + count_tokens(human)

    refresh_story()
    if tokens() > max_tokens and drop_previous_scene_tail():
        refresh_story()
    while tokens() > max_tokens and len(history) > 1:
        # 保留最新一条真实记录，较早回合只按整条记录淘汰。
        history.pop(0)
        refresh_story()
    if tokens() > max_tokens:
        # 固定角色、两幕摘要和玩家本轮输入都不能被裁成半句。
        raise NumericV2ActorError("numeric_v2_actor_fixed_context_budget_exceeded")
    if diagnostics is not None:
        included_history_revisions = [
            row.get("revision")
            for row in history
            if isinstance(row.get("revision"), int)
        ]
        diagnostics.clear()
        diagnostics.update({
            "budget_tokens": max_tokens,
            "final_tokens": tokens(),
            "history_included_revisions": included_history_revisions,
            "history_dropped_revisions": [
                revision
                for revision in initial_history_revisions
                if revision not in included_history_revisions
            ],
        })
    return fitted


def _log_prompt_diagnostics(session: ScriptSessionV2, diagnostics: Mapping[str, Any]) -> None:
    """只记录装箱版本号和 token，不把玩家正文写入日志。"""  # noqa: DOCSTRING_CJK

    message = (
        "Numeric v2 Actor prompt packing session_id=%s revision=%s "
        "tokens=%s/%s history_in=%s history_drop=%s"
    )
    args = (
        session.session_id,
        session.revision,
        diagnostics.get("final_tokens"),
        diagnostics.get("budget_tokens"),
        diagnostics.get("history_included_revisions"),
        diagnostics.get("history_dropped_revisions"),
    )
    if diagnostics.get("history_dropped_revisions"):
        logger.info(message, *args)
    else:
        logger.debug(message, *args)


def _transition_prompt_data(
    *,
    story_context: Mapping[str, Any],
    player_address: str,
    current_chapter_title: str,
    target_chapter_title: str,
    shared_boundaries: list[Any],
    source_boundaries: list[Any],
    target_boundaries: list[Any],
    runtime_target_opening: str,
    target_story_direction: str,
    transition_contract: Mapping[str, Any],
    recent_context: list[dict[str, Any]],
    acting_context: Mapping[str, Any],
    player_input: str,
) -> dict[str, Any]:
    """直接构造换场工作记忆，不先生成随后会被删除的完整普通回合上下文。"""  # noqa: DOCSTRING_CJK

    transition = {
        "authorized": True,
        "source_scene": {
            "chapter_title": str(current_chapter_title or ""),
            "boundaries": list(source_boundaries),
        },
        "target_scene": {
            "chapter_title": str(target_chapter_title or ""),
            "opening_situation": str(runtime_target_opening or ""),
            "story_direction": str(target_story_direction or ""),
            "boundaries": list(target_boundaries),
        },
        "shared_boundaries": list(shared_boundaries),
        "reason": str(transition_contract.get("reason") or ""),
        "must_deliver": list(transition_contract.get("must_deliver") or []),
        "bridge_scene_narration": str(
            transition_contract.get("bridge_scene_narration") or ""
        ),
        "must_preserve": list(transition_contract.get("must_preserve") or []),
        "tone": str(transition_contract.get("tone") or ""),
    }
    return {
        "story_context": dict(story_context),
        "player_address": str(player_address or "你"),
        "recent_context": list(recent_context),
        "acting_context": dict(acting_context),
        "player_input": str(player_input or ""),
        "transition": transition,
    }


def _opening_messages(
    engine: NumericV2Engine,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
    player_address_known: bool = True,
    max_tokens: int = 4800,
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    node = engine.nodes[str(engine.story["start_node_id"])]
    opening_beat = _opening_beat_for_actor(engine, cast, node)
    # 正文 Actor 不接收玩家待办；推荐只根据已生成正文单独补全，避免职责混入。
    opening_beat = dict(opening_beat)
    opening_beat.pop("player_reply_goals", None)
    opening_beat.pop("current_direction", None)
    data = {
        "opening_phase": True,
        "visible_player_history": [],
        "player_address_state": {
            "known": player_address_known,
            "projection": player_address,
        },
        "story_context": _story_context_for_actor(
            cast,
            engine.story,
            beats=(node["story_beat"],),
        ),
        "current_chapter_title": _chapter_title_for_actor(cast, node),
        "current_story_beat": opening_beat,
        "acting_context": _acting_context(
            engine,
            cast,
            node,
            engine.story["initial_state"]["metrics"],
            character_profile,
            dialogue_policy=str(
                _acting_contract_for_actor(cast, node["story_beat"]).get(
                    "dialogue_policy"
                )
                or "required"
            ),
        ),
        "instruction": (
            "这是玩家输入前的公开开场。使用必要的环境或猫娘可见行动建立当下场景，再由猫娘主动说出第一句；"
            "不得假定玩家已经说话、做出选择或完成无前因的主动行动，不得使用‘你刚才说/做’或同义的隐形前史。"
            "若 opening_scene 同句明确给出可见前因，可以建立玩家受伤、失衡或被外力带动等即时身体结果；"
            "若节点摘要只有玩家台词、决定或无前因主动行为，把它们视为后续可发展的剧情边界，不要在开场代替玩家执行。"
            "猫娘对白必须由本段旁白能够直接解释，并留下男主可以自然回应的话头；"
            "话头不得反问玩家来替猫娘确认 opening_deliverables 已经要求她肯定确认的状态；不要提前演完本节点。"
        ),
    }
    return _ensure_actor_messages_fit(
        [
            SystemMessage(content=_system_prompt(
                catgirl_name=catgirl_name,
                player_address=player_address,
                player_address_known=player_address_known,
                phase="opening",
            )),
            HumanMessage(content=json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
        ],
        max_tokens=max_tokens,
    )


def _turn_messages(
    engine: NumericV2Engine,
    session: ScriptSessionV2,
    outcome: TurnOutcomeV2,
    player_input: str,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
    player_address_known: bool = True,
    deterministic_transition: bool = False,
    retry_hint: str = "",
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    source = engine.nodes[str(outcome.ledger_event["from_node_id"])]
    target = engine.nodes[str(outcome.ledger_event["to_node_id"])]
    route_changed = source["id"] != target["id"]
    system_prompt = _system_prompt(
        catgirl_name=catgirl_name,
        player_address=player_address,
        player_address_known=player_address_known,
        phase=(
            "transition_compact"
            if route_changed and deterministic_transition
            else ("transition" if route_changed else "turn")
        ),
    )
    if retry_hint:
        # 重试时明确要求改写当前回应，避免同一输入和同一上下文连续生成相同正文。
        system_prompt += f"\n本轮是输出重试：{retry_hint}"
    current_player_input = str(player_input or "")
    human_prefix = "以下 JSON 是已确定性结算的本回合数据：\n"
    soft_pacing = _soft_pacing(
        source,
        session.node_turn_count + 1,
        route_changed=route_changed,
        transition_intent=str(
            outcome.ledger_event.get("transition_intent") or "unclear"
        ),
    )
    story_context = _story_context_for_actor(
        cast,
        engine.story,
        beats=(
            (source["story_beat"], target["story_beat"])
            if route_changed
            else (source["story_beat"],)
        ),
    )
    actor_budget = numeric_v2_actor_budget(session.actor_budget_profile)
    history_selection_diagnostics: dict[str, Any] = {}
    # 普通回合默认保留当前幕完整历史，只有上下文预算不足时才由六块装箱器从最早整轮开始压缩；
    # 正式换场继续使用原有回合窗口，避免扩大高风险协议的改动范围。
    recent_context = _history(
        session,
        max_tokens=actor_budget["history_max_tokens"],
        max_turns=(
            actor_budget["history_max_turns"]
            if route_changed
            else None
        ),
        # 只有进入新幕后尚未演过普通回合时，才临时承接上一幕已提交的可见余波。
        include_previous_scene_tail=(not route_changed and session.node_turn_count == 0),
        diagnostics=history_selection_diagnostics,
    )
    if route_changed:
        source_transition_dialogue_policy = transition_source_dialogue_policy(
            session.dialogue_policy
        )
        # 换场直接构造紧凑工作记忆，不创建普通回合字段后再二次裁剪。
        source_beat = _beat_for_actor(cast, source["story_beat"])
        target_beat = _beat_for_actor(cast, target["story_beat"])
        # 两幕完全相同的硬边界只发送一次；精确字符串去重不猜语义，来源和目标差异仍分别完整保留。
        source_boundaries = list(source_beat.get("boundaries") or [])
        target_boundaries = list(target_beat.get("boundaries") or [])
        shared_boundaries = [
            boundary
            for boundary in source_boundaries
            if boundary in target_boundaries
        ]
        source_boundaries = [
            boundary
            for boundary in source_boundaries
            if boundary not in shared_boundaries
        ]
        target_boundaries = [
            boundary
            for boundary in target_boundaries
            if boundary not in shared_boundaries
        ]
        system_prompt += _hard_boundary_system_instruction([
            *(f"来源幕：{item}" for item in source_boundaries),
            *(f"目标幕：{item}" for item in target_boundaries),
            *shared_boundaries,
        ])
        target_opening = str(target_beat["opening_scene"])
        transition_contract = _transition_contract_for_actor(
            cast,
            outcome.transition_contract,
            target_opening=target_opening,
        )
        data = _transition_prompt_data(
            story_context=story_context,
            player_address=player_address,
            current_chapter_title=_chapter_title_for_actor(cast, source),
            target_chapter_title=_chapter_title_for_actor(cast, target),
            shared_boundaries=shared_boundaries,
            source_boundaries=source_boundaries,
            target_boundaries=target_boundaries,
            runtime_target_opening=target_opening,
            target_story_direction=str(target_beat.get("scene_direction") or ""),
            transition_contract=transition_contract,
            recent_context=recent_context,
            acting_context=_acting_context(
                engine,
                cast,
                source,
                outcome.session.metrics,
                character_profile,
                relationship_metrics=session.metrics,
                target=target,
                dialogue_policy=source_transition_dialogue_policy,
                target_dialogue_policy=outcome.session.dialogue_policy,
            ),
            player_input=current_player_input,
        )
        final_transition = data.pop("transition")
        data["transition"] = final_transition
        packing_diagnostics: dict[str, Any] = {}
        data = _fit_turn_prompt_data(
            system_prompt=system_prompt,
            human_prefix=human_prefix,
            data=data,
            max_tokens=actor_budget["input_max_tokens"],
            diagnostics=packing_diagnostics,
        )
        packing_diagnostics["history_available_revisions"] = (
            history_selection_diagnostics["history_available_revisions"]
        )
        packing_diagnostics["history_dropped_revisions"] = list(dict.fromkeys((
            *history_selection_diagnostics["history_preselection_dropped_revisions"],
            *packing_diagnostics["history_dropped_revisions"],
        )))
        _log_prompt_diagnostics(session, packing_diagnostics)
        return [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prefix + json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
        ]

    role_context = _acting_context(
        engine,
        cast,
        source,
        outcome.session.metrics,
        character_profile,
        relationship_metrics=session.metrics,
        target=None,
        dialogue_policy=outcome.session.dialogue_policy,
    )
    # 当前幕同时给出已进入的开场处境和作者完整方向。方向不是已发生事实或目标清单，
    # 但不能只剩开场画面，否则模型在长对话或跑题后会失去本幕真正的因果线。
    projected_source_beat = cast.value(source["story_beat"])
    current_scene_opening = cast.text(
        scene_narrative_summary(projected_source_beat)
    ).strip()
    current_scene_direction = cast.text(
        str(
            projected_source_beat.get("narrative_summary")
            or projected_source_beat.get("summary")
            or scene_narrative_focus(projected_source_beat)
            or ""
        )
    ).strip()
    if current_scene_direction and current_scene_direction != current_scene_opening:
        current_scene_summary = (
            f"当前已进入的开场处境：{current_scene_opening}\n"
            "本幕完整剧情方向（自然演绎，不是任务清单，也不是已发生事实）："
            f"{current_scene_direction}"
        )
    else:
        current_scene_summary = current_scene_opening
    relationship_control = role_context.get("relationship_control")
    relationship_boundary = (
        str(relationship_control.get("response_contract") or "").strip()
        if isinstance(relationship_control, Mapping)
        else ""
    )
    hard_boundaries = _suggestion_hard_boundaries(
        cast,
        source["story_beat"],
        relationship_boundary=relationship_boundary,
    )
    # 精确边界放进 System 合同以提高遵循度，Human 六块继续只承载角色、剧情、历史与当前输入，避免重复 Token。
    system_prompt += _hard_boundary_system_instruction(hard_boundaries)
    next_scene_preview = _next_scene_preview_for_actor(
        engine,
        cast,
        source,
        outcome.session.metrics,
    )
    pacing_text = (
        f"当前是第 {int(soft_pacing['current_turn'])} 回合，本幕推荐 "
        f"{int(soft_pacing['recommended_turns'])} 回合。"
        f"{str(soft_pacing['instruction'])}"
        "事实纪律：current_scene 已明确的地点关系、通道与行动流程优先于玩家的猜测性说法；"
        "玩家说‘可能有’、询问未知对象或提出假设时，不得顺势确认并新增岔路、机关、障碍或检查步骤。"
        "玩家问‘会不会’、‘是不是’或说‘好像’、‘感觉’时，不能把被询问的可能性写成猫娘已观测到的环境事实。"
        "玩家提出具体型号、接口、结构、能力、适配或运行结果时，若作者已明确保持未知、禁止虚构或已有事实与其冲突，"
        "它仍只是待验证假设；不得通过一次试插、搜索、观察或角色感觉就把它写成存在、匹配或成功。"
        "某件事已明确不知道时，猫娘说完‘不知道’后不得再用‘好像’、‘感觉’、‘也许’补造一个具体机制；"
        "只能保持未知、提出已知安全的核对方式，或转向 current_scene 已经支持的下一个事件。"
        "已有明确通道或连续流程且没有出现新的作者事实风险时，直接推进到本轮当前幕内结果。"
        "玩家用玩笑、闲聊或关系回应短暂跑题时，先自然回应一句，再承接 story_so_far 中尚未收住的当前因果线；"
        "跑题本身不表示当前互动已经完成，也不能成为新转场提议的理由。"
        "next_scene 独有而 current_scene 与 story_so_far 尚未发生的目的地、任务、消息或设备不能出现在正文或推荐中，"
        "更不能被反向当成当前转场的因果证据。"
        "演绎职责：玩家只声明自己的话、选择和行动；当前环境、NPC 与行动结果由你依据已知事实演出。"
        "玩家观察、搜索、按下、打开、救援或操作后，不得反问玩家看见、听见、摸到或发生了什么，"
        "也不得推荐玩家替你宣告发现、物品、成功或环境结果；若作者事实不足以确定结果，就明确保持未知并给出不依赖新事实的选择。"
        "最近两轮若已在执行同一连续行动且没有新风险选择，本轮应把它推进到下一个真正需要决定的位置；"
        "推荐第一条也应执行这段完整行动，不能继续推荐靠近一步、再听一次、再确认同一对象。"
        "输出前最后自检：suggested_inputs 每条都必须同时有玩家动作和玩家对白；"
        "动作可省略‘我’，但不能明写其他人或环境为主体。"
    )
    # 叙事重心只作为一条创作方向提示，不转化为目标、证据或 Runtime 门槛。
    narrative_focus = cast.text(scene_narrative_focus(source["story_beat"])).strip()
    if narrative_focus:
        pacing_text += f"当前叙事重心：{narrative_focus}。"
    if (
        not route_changed
        and not session.transition_offered
        and int(soft_pacing["current_turn"]) >= int(soft_pacing["recommended_turns"])
    ):
        pacing_text += (
            "先对照 current_scene 的完整剧情方向与 story_so_far 的当前幕事实索引：这不是逐项目标检查，"
            "只判断本幕的核心冲突或选择是否已经让玩家看懂。若尚未建立，应直接用当前环境或角色能自然给出的内容"
            "交付一项最关键的作者方向事实，本轮不要提离幕；不得另造需要多轮搜索、试错或解锁的中间物、卡扣、暗门、"
            "工具、机关或障碍。若核心冲突已经清楚，就不要再开启新的幕内问题，应把当前连续行动推进到结果或自然出口。"
        )
        if bool(next_scene_preview.get("target_is_ending")):
            pacing_text += (
                "下一幕是结局收束，但地点不一定改变：若当前危机或互动阶段已经自然收住，可以提出结束危机、"
                "转入休养、开始记录或确认选择等具体收束动作；若尚未收住，继续交付当前行动结果，不得为了结局硬凑提议。"
                "不要提前描写结局独有的地点、时间推进、生活状态或最终结果。"
            )
    if session.transition_offered and not route_changed:
        # 上一轮已有可见提议但本轮还没有正式换幕时，禁止把目标地点或目标结果写成已发生。
        pacing_text += (
            "上一轮正文已经提出具体转场，但本回合尚未完成换幕；只能停留在当前幕回应。"
            "可以写准备、观察或等待，不得把目标地点写成已经抵达，不得替玩家完成尚未确认的行动。"
            "推荐输入必须包含一条明确接受并亲自执行上一轮具体提议的路径，且必须放在第一条；"
            "第二条必须是明确拒绝、暂缓或当前幕替代路径；如有第三条，也要是不同的实际行动。"
            "所有路径都要直接承接正文，不要只写泛泛安慰或重复提问。"
        )
        pending_offer = pending_transition_performance(session)
        if pending_offer:
            # 把玩家已经看到的具体提议从长历史中单独提亮，帮助推荐生成真正可执行的接受路径。
            pacing_text += f"上一轮具体提议原文（仅供玩家回应）：{pending_offer}"
    elif (
        not route_changed
        and isinstance(soft_pacing.get("recommended_turns"), int)
        and int(soft_pacing["current_turn"]) >= int(soft_pacing["recommended_turns"])
    ):
        # 软收束线上的新提议也要同时给出接受和替代路径，避免压测或玩家只能猜哪条会换幕。
        pacing_text += (
            "如果本轮正文提出了具体离幕提议，推荐输入必须把明确接受并亲自执行该提议的路径放在第一条，"
            "第二条提供明确拒绝、暂缓或当前幕替代路径；不要把两条推荐写成同一种行动。"
        )
    # 六块数据按固定顺序写入，玩家输入始终位于最后，减少历史内容覆盖当前要求。
    scene_fact_index = _current_scene_fact_index_text(session)
    story_prefix = (
        "当前幕已提交事实索引（只用于防止长幕后倒退，不是任务清单）：\n"
        f"{scene_fact_index}\n\n最近完整对话："
        if scene_fact_index
        else ""
    )
    data: dict[str, str] = {
        "role": _role_prompt_text(
            catgirl_name=catgirl_name,
            acting_context=role_context,
        ),
        "current_scene": current_scene_summary,
        "story_so_far": _story_so_far_text(recent_context),
        "pacing": pacing_text,
        "next_scene": _next_scene_summary_text(next_scene_preview),
        "player_input": current_player_input,
    }
    human_prefix = "以下 JSON 是本回合六块演绎上下文：\n"
    packing_diagnostics = {}
    data = _fit_simple_turn_prompt_data(
        system_prompt=system_prompt,
        human_prefix=human_prefix,
        data=data,
        history_rows=recent_context,
        max_tokens=actor_budget["input_max_tokens"],
        story_prefix=story_prefix,
        diagnostics=packing_diagnostics,
    )
    packing_diagnostics["history_available_revisions"] = (
        history_selection_diagnostics["history_available_revisions"]
    )
    packing_diagnostics["history_dropped_revisions"] = list(dict.fromkeys((
        *history_selection_diagnostics["history_preselection_dropped_revisions"],
        *packing_diagnostics["history_dropped_revisions"],
    )))
    _log_prompt_diagnostics(session, packing_diagnostics)
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prefix + json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    ]


async def _model_config(config_manager: Any) -> dict[str, Any]:
    getter = getattr(config_manager, "aget_model_api_config", None) or getattr(config_manager, "get_model_api_config", None)
    if getter is None:
        raise NumericV2ActorUnavailableError("numeric_v2_actor_config_unavailable")
    try:
        value = getter("conversation")
        config = await value if inspect.isawaitable(value) else value
    except Exception as exc:
        raise NumericV2ActorUnavailableError("numeric_v2_actor_config_unavailable") from exc
    if not isinstance(config, Mapping) or not str(config.get("model") or "").strip() or not str(config.get("base_url") or "").strip():
        raise NumericV2ActorUnavailableError("numeric_v2_actor_config_unavailable")
    return dict(config)


class NumericV2Actor:
    """编排一次 Actor 输出正文、推荐与转场意图；不拥有 Session 写权限。"""  # noqa: DOCSTRING_CJK

    def __init__(self, config_manager: Any):
        self.config_manager = config_manager
        # 统计该 Actor 实例真正进入供应商请求的次数；正文重采样和轻量补推荐都会分别计数。
        self.provider_call_count = 0
        # 区分补推荐的触发原因与真实供应商成本，便于压测判断哪类调用可以优化。
        self.suggestion_fill_attempt_count = 0
        self.suggestion_fill_provider_call_count = 0
        self.suggestion_fill_reason_counts = {
            "invalid_or_missing": 0,
            "transition_refresh": 0,
            "scene_refresh": 0,
        }
        self.base_suggestion_parse_counts: dict[str, int] = {}

    def _character_profile(self) -> str:
        """只读取服务端当前猫娘的人格摘要，不接受客户端角色名。"""  # noqa: DOCSTRING_CJK

        try:
            characters = self.config_manager.load_characters()
        except Exception:
            characters = {}
        current_name = str(characters.get("当前猫娘") or "").strip() if isinstance(characters, Mapping) else ""
        return _load_character_profile(
            self.config_manager,
            current_name,
        )

    def _current_catgirl_name(self) -> str:
        try:
            characters = self.config_manager.load_characters()
        except Exception:
            return "当前猫娘"
        return str(characters.get("当前猫娘") or "当前猫娘").strip() or "当前猫娘"

    async def _ensure_suggestions(
        self,
        *,
        performance: Mapping[str, Any],
        player_input: str,
        catgirl_name: str,
        max_input_tokens: int,
        hard_boundaries: list[str] | tuple[str, ...] = (),
        force_refresh: bool = False,
    ) -> list[str]:
        """推荐格式异常、已提转场或换幕后补一次推荐，不覆盖合法正文。"""  # noqa: DOCSTRING_CJK

        suggestions = [
            str(item).strip()
            for item in performance.get("suggested_inputs") or []
            if str(item).strip()
        ]
        transition_offered = performance.get("transition_offered") is True
        if len(suggestions) in {2, 3} and not transition_offered and not force_refresh:
            return suggestions
        if force_refresh:
            fill_reason = "scene_refresh"
        elif transition_offered:
            fill_reason = "transition_refresh"
        else:
            fill_reason = "invalid_or_missing"
        self.suggestion_fill_attempt_count += 1
        self.suggestion_fill_reason_counts[fill_reason] += 1
        provider_calls_before = self.provider_call_count
        try:
            filled = await self._invoke(
                _suggestion_fill_messages(
                    catgirl_name=catgirl_name,
                    performance=performance,
                    player_input=player_input,
                    max_tokens=max_input_tokens,
                    transition_offered=transition_offered,
                    hard_boundaries=hard_boundaries,
                ),
                suggestions_only=True,
                transition_suggestions_only=transition_offered,
                max_input_tokens=max_input_tokens,
                max_output_tokens=NUMERIC_V2_ACTOR_SUGGESTION_FILL_MAX_OUTPUT_TOKENS,
            )
        except NumericV2ActorError as exc:
            # 补推荐失败不能回滚正文；已有合法推荐时继续保留，避免一次辅助调用抖动清空按钮。
            logger.warning(
                "Numeric v2 Actor suggestion fill failed: reason=%s",
                str(exc),
            )
            return suggestions if len(suggestions) in {2, 3} else []
        finally:
            self.suggestion_fill_provider_call_count += max(
                0,
                self.provider_call_count - provider_calls_before,
            )
        filled_suggestions = [
            str(item).strip()
            for item in filled.get("suggested_inputs") or []
            if str(item).strip()
        ]
        if len(filled_suggestions) in {2, 3}:
            return filled_suggestions
        return suggestions if len(suggestions) in {2, 3} else []

    async def generate_opening(
        self,
        *,
        engine: NumericV2Engine,
        actor_budget_profile: str = NUMERIC_V2_DEFAULT_ACTOR_BUDGET_PROFILE,
    ) -> dict[str, Any]:
        actor_budget = numeric_v2_actor_budget(actor_budget_profile)
        profile = self._character_profile()
        catgirl_name = self._current_catgirl_name()
        configured_address = _load_player_address(self.config_manager)
        player_address_known = bool(engine.story["initial_state"]["player_address_known"])
        player_address = _project_player_address(
            configured_address,
            known=player_address_known,
        )
        start_node = engine.nodes[str(engine.story["start_node_id"])]
        start_cast = NumericV2CastProjection.from_story(
            engine.story,
            player_name=player_address,
            catgirl_name=catgirl_name,
        )
        start_contract = _acting_contract_for_actor(start_cast, start_node["story_beat"])
        opening_dialogue_policy = str(
            start_contract.get("dialogue_policy") or "required"
        )
        performance = await self._invoke(
            _opening_messages(
                engine,
                profile,
                catgirl_name,
                player_address,
                player_address_known,
                max_tokens=actor_budget["input_max_tokens"],
            ),
            opening_required=True,
            max_input_tokens=actor_budget["input_max_tokens"],
            max_output_tokens=NUMERIC_V2_ACTOR_OPENING_MAX_OUTPUT_TOKENS,
            dialogue_policy=opening_dialogue_policy,
        )
        _assert_acting_contract_output(
            performance,
            character_profile=profile,
            acting_contract=start_contract,
        )
        _assert_no_unknown_player_address_leak(
            performance,
            player_address=configured_address,
            player_address_known=player_address_known,
        )
        performance = dict(performance)
        performance["suggested_inputs"] = await self._ensure_suggestions(
            performance=performance,
            player_input="",
            catgirl_name=catgirl_name,
            max_input_tokens=actor_budget["input_max_tokens"],
            hard_boundaries=_suggestion_hard_boundaries(
                start_cast,
                start_node["story_beat"],
                relationship_boundary=str(
                    _relationship_control(
                        engine,
                        start_node,
                        engine.story["initial_state"]["metrics"],
                    ).get("response_contract")
                    or ""
                ),
            ),
        )
        return performance

    async def generate_turn(
        self,
        *,
        engine: NumericV2Engine,
        session: ScriptSessionV2,
        outcome: TurnOutcomeV2,
        player_input: str,
        character_profile: str | None = None,
        retry_hint: str = "",
    ) -> dict[str, Any]:
        # 工作流可冻结本轮实际使用的人格文本，确保提交前能复验同一生成世代。
        profile = (
            self._character_profile()
            if character_profile is None
            else str(character_profile)
        )
        catgirl_name = str(session.catgirl_binding.get("catgirl_name") or self._current_catgirl_name())
        configured_address = str(
            session.catgirl_binding.get("player_address")
            or _load_player_address(self.config_manager)
        ).strip()
        player_address_known = session.player_address_known
        player_address = _project_player_address(
            configured_address,
            known=player_address_known,
        )
        route_changed = (
            outcome.ledger_event["from_node_id"]
            != outcome.ledger_event["to_node_id"]
        )
        source = engine.nodes[str(outcome.ledger_event["from_node_id"])]
        target = engine.nodes[str(outcome.ledger_event["to_node_id"])]
        cast = NumericV2CastProjection.from_story(
            engine.story,
            player_name=player_address,
            catgirl_name=catgirl_name,
        )
        target_beat = _beat_for_actor(cast, target["story_beat"])
        target_opening = target_beat["opening_scene"]
        transition_contract = (
            _transition_contract_for_actor(
                cast,
                outcome.transition_contract,
                target_opening=target_opening,
            )
            if route_changed
            else {}
        )
        authored_bridge = str(
            transition_contract.get("bridge_scene_narration") or ""
        ).strip()
        deterministic_transition = bool(route_changed and authored_bridge)
        source_transition_dialogue_policy = transition_source_dialogue_policy(
            session.dialogue_policy
        )
        visible_contract = _acting_contract_for_actor(
            cast,
            (target if route_changed else source)["story_beat"],
        )
        actor_budget = numeric_v2_actor_budget(session.actor_budget_profile)
        performance = await self._invoke(
            _turn_messages(
                engine,
                session,
                outcome,
                player_input,
                profile,
                catgirl_name,
                player_address,
                player_address_known,
                deterministic_transition,
                retry_hint,
            ),
            transition_required=route_changed,
            deterministic_transition=deterministic_transition,
            max_input_tokens=actor_budget["input_max_tokens"],
            max_output_tokens=(
                NUMERIC_V2_ACTOR_TRANSITION_MAX_OUTPUT_TOKENS
                if route_changed
                else NUMERIC_V2_ACTOR_TURN_MAX_OUTPUT_TOKENS
            ),
            target_opening=target_opening,
            dialogue_policy=outcome.session.dialogue_policy,
            source_dialogue_policy=source_transition_dialogue_policy,
            target_dialogue_policy=outcome.session.dialogue_policy,
        )
        if route_changed:
            # 先统一为提交合同，再执行所有权、人格和事实检查；两种 Actor 输出形状因此共享同一验证路径。
            performance = engine.finalize_transition_performance(
                outcome,
                performance,
                target_opening=target_opening,
                bridge_required=bool(transition_contract.get("bridge_required")),
                bridge_scene_narration=authored_bridge,
                source_dialogue_policy=source_transition_dialogue_policy,
                target_dialogue_policy=outcome.session.dialogue_policy,
            )
            segments = performance.get("segments")
            source_segment = segments[0] if isinstance(segments, list) and len(segments) == 3 else {}
            target_segment = segments[2] if isinstance(segments, list) and len(segments) == 3 else {}
            # 人格合同只检查 Actor 生成的两侧正文，不能把 Runtime 注入的作者开场误判成模型越权。
            source_output = {"performance": source_segment.get("performance", "")}
            target_output = {"performance": target_segment.get("performance", "")}
            _assert_acting_contract_output(
                source_output,
                character_profile=profile,
                acting_contract=_acting_contract_for_actor(cast, source["story_beat"]),
            )
            _assert_acting_contract_output(
                target_output,
                character_profile=profile,
                acting_contract=_acting_contract_for_actor(cast, target["story_beat"]),
            )
        else:
            _assert_acting_contract_output(
                performance,
                character_profile=profile,
                acting_contract=_acting_contract_for_actor(cast, source["story_beat"]),
            )
        _assert_no_unknown_player_address_leak(
            performance,
            player_address=configured_address,
            player_address_known=player_address_known,
            player_input=player_input,
        )
        # 第一回合没有普通历史时，opening 就是玩家上一条看到的演绎；重复保护必须覆盖它。
        if session.performance_history:
            previous_visible_performance = session.performance_history[-1]
            previous_scene_performance = previous_visible_performance
        else:
            previous_visible_performance = session.opening_performance
            previous_scene_performance = previous_visible_performance
            if isinstance(previous_visible_performance.get("performance"), str):
                # 开场场景旁白不会在普通回合复现；比较时只取猫娘开场正文，避免长旁白稀释重复率。
                previous_visible_performance = {
                    "performance": previous_visible_performance["performance"],
                }
        if not route_changed:
            performance = _deduplicate_scene_update(
                performance,
                previous_scene_performance,
            )
        if _repeats_earlier_session_performance(
            performance,
            session,
            route_changed=route_changed,
        ):
            logger.warning(
                "Numeric v2 Actor failed: reason=numeric_v2_actor_repeated_session_output session_id=%s revision=%s",
                session.session_id,
                session.revision,
            )
            raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
        if (
            previous_visible_performance
            and route_changed
            and _transition_source_repeats_previous(
                performance,
                previous_visible_performance,
            )
        ):
            logger.warning(
                "Numeric v2 Actor failed: reason=numeric_v2_actor_repeated_transition_source session_id=%s revision=%s",
                session.session_id,
                session.revision,
            )
            raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
        if (
            previous_visible_performance
            and _is_repeated_performance(performance, previous_visible_performance)
        ):
            stable_confirmation = (
                not route_changed
                and not outcome.metric_changes
                and "scene_narration" not in performance
                and count_tokens(_performance_text(performance)) <= 80
                and _player_input_repeats_recent_context(
                    player_input,
                    _history(
                        session,
                        max_tokens=actor_budget["history_max_tokens"],
                        max_turns=actor_budget["history_max_turns"],
                    ),
                )
            )
            if stable_confirmation:
                # 玩家重复同一输入且状态没有任何变化时，简短确认是合理结果；
                # 依然禁止携带新场景事实或正在交付目标的重复段落。
                logger.debug(
                    "Numeric v2 Actor accepted stable repeated confirmation: session_id=%s revision=%s",
                    session.session_id,
                    session.revision,
                )
            else:
                logger.warning(
                    "Numeric v2 Actor failed: reason=numeric_v2_actor_repeated_output session_id=%s revision=%s",
                    session.session_id,
                    session.revision,
                )
                raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
        # 正文通过全部确定性校验后，再确保同一次 Actor 调用返回的推荐满足数量合同；
        # 仅在推荐异常时发起一次轻量补全，不重新生成或覆盖正文。
        performance = dict(performance)
        performance["suggested_inputs"] = await self._ensure_suggestions(
            performance=performance,
            player_input=player_input,
            catgirl_name=catgirl_name,
            max_input_tokens=actor_budget["input_max_tokens"],
            # 换幕主调用中的推荐可能在生成来源回应时已定型；
            # 目标幕入场后强制用最终可见开场轻量刷新，避免按钮继续执行旧幕离开动作。
            force_refresh=route_changed,
            hard_boundaries=_suggestion_hard_boundaries(
                cast,
                (target if route_changed else source)["story_beat"],
                relationship_boundary=str(
                    _relationship_control(
                        engine,
                        target if route_changed else source,
                        session.metrics,
                    ).get("response_contract")
                    or ""
                ),
            ),
        )
        return performance

    async def _invoke(
        self,
        messages: list[Any],
        *,
        opening_required: bool = False,
        transition_required: bool = False,
        deterministic_transition: bool = False,
        max_input_tokens: int = 4800,
        max_output_tokens: int = NUMERIC_V2_ACTOR_TURN_MAX_OUTPUT_TOKENS,
        target_opening: str = "",
        dialogue_policy: str = "required",
        source_dialogue_policy: str = "required",
        target_dialogue_policy: str = "required",
        suggestions_only: bool = False,
        transition_suggestions_only: bool = False,
    ) -> dict[str, Any]:
        set_call_type("theater_numeric_v2_actor")
        request_messages = _ensure_actor_messages_fit(
            messages,
            max_tokens=max_input_tokens,
        )
        started_at = time.monotonic()
        config_finished_at = started_at
        client_finished_at = started_at
        request_finished_at = started_at
        try:
            # 总时限覆盖配置读取、客户端构造、网络请求、输出解析和客户端关闭。
            async with asyncio.timeout(NUMERIC_V2_ACTOR_TIMEOUT_SECONDS):
                config = await _model_config(self.config_manager)
                config_finished_at = time.monotonic()
                client = await create_chat_llm_async(
                    str(config["model"]),
                    str(config["base_url"]),
                    config.get("api_key"),
                    provider_type=config.get("provider_type"),
                    timeout=NUMERIC_V2_ACTOR_TIMEOUT_SECONDS,
                    max_retries=0,
                    max_completion_tokens=max_output_tokens,
                )
                client_finished_at = time.monotonic()
                async with client:
                    # request_messages 已由 _ensure_actor_messages_fit 按模型输入预算裁剪。
                    self.provider_call_count += 1
                    response = await client.ainvoke(request_messages)  # noqa: LLM_INPUT_BUDGET
                    request_finished_at = time.monotonic()
                    suggestion_diagnostics: dict[str, int] = {}
                    parsed = _parse_output(
                        getattr(response, "content", None),
                        opening_required=opening_required,
                        transition_required=transition_required,
                        deterministic_transition=deterministic_transition,
                        target_opening=target_opening,
                        dialogue_policy=dialogue_policy,
                        source_dialogue_policy=source_dialogue_policy,
                        target_dialogue_policy=target_dialogue_policy,
                        suggestions_only=suggestions_only,
                        transition_suggestions_only=transition_suggestions_only,
                        suggestion_diagnostics=suggestion_diagnostics,
                    )
                    if not suggestions_only and not transition_suggestions_only:
                        for reason, count in suggestion_diagnostics.items():
                            self.base_suggestion_parse_counts[reason] = (
                                self.base_suggestion_parse_counts.get(reason, 0)
                                + int(count)
                            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Numeric v2 Actor failed: reason=numeric_v2_actor_timeout elapsed=%.3f",
                time.monotonic() - started_at,
            )
            raise NumericV2ActorError("numeric_v2_actor_timeout") from exc
        except NumericV2ActorError as exc:
            reason = str(exc) if str(exc).startswith("numeric_v2_actor_") else type(exc).__name__
            logger.warning("Numeric v2 Actor failed: reason=%s", reason)
            raise
        except Exception as exc:
            logger.warning("Numeric v2 Actor failed: reason=numeric_v2_actor_model_call_failed error_type=%s", type(exc).__name__)
            raise NumericV2ActorError("numeric_v2_actor_model_call_failed") from exc
        total_seconds = time.monotonic() - started_at
        if total_seconds >= NUMERIC_V2_ACTOR_SLOW_CALL_SECONDS:
            # 只记录阶段耗时，不记录剧本、玩家输入或模型输出。
            logger.warning(
                "Numeric v2 Actor slow call: total=%.3f config=%.3f client=%.3f request=%.3f finalize=%.3f",
                total_seconds,
                config_finished_at - started_at,
                client_finished_at - config_finished_at,
                request_finished_at - client_finished_at,
                time.monotonic() - request_finished_at,
            )
        return parsed


__all__ = [
    "NUMERIC_V2_ACTOR_OPENING_MAX_OUTPUT_TOKENS",
    "NUMERIC_V2_ACTOR_TRANSITION_MAX_OUTPUT_TOKENS",
    "NUMERIC_V2_ACTOR_TURN_MAX_OUTPUT_TOKENS",
    "NUMERIC_V2_ACTOR_TIMEOUT_SECONDS",
    "NumericV2Actor",
    "NumericV2ActorError",
    "NumericV2ActorOutputError",
    "NumericV2ActorUnavailableError",
]
