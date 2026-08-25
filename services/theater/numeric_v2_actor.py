"""Numeric v2 演绎 Actor，只生成表现文本和玩家建议。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import inspect
import json
import logging
import re
import time
from typing import Any, Mapping

from config.prompts.prompts_theater import NUMERIC_V2_ACTOR_JSON_INSTRUCTION
from utils.llm_client import HumanMessage, SystemMessage, create_chat_llm_async
from utils.token_tracker import set_call_type
from utils.tokenize import count_tokens

from .llm_context import (
    _load_character_profile,
    _load_player_address,
)
from .numeric_v2 import numeric_v2_story_goal_contracts
from .numeric_v2_cast import NumericV2CastProjection
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
)
from .numeric_v2_runtime import NumericV2Engine, ScriptSessionV2, TurnOutcomeV2


NUMERIC_V2_ACTOR_TIMEOUT_SECONDS = 35.0
NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS = 1600
NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS = 4800
NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS = 2200
NUMERIC_V2_ACTOR_REPEAT_SIMILARITY = 0.65
NUMERIC_V2_ACTOR_SUGGESTION_REPEAT_SIMILARITY = 0.8
NUMERIC_V2_ACTOR_SLOW_CALL_SECONDS = 15.0
NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION = (
    "In a performance string, text inside Chinese full-width parentheses is a visible "
    "micro-action and all text outside parentheses is spoken dialogue by the active catgirl. "
    "Actions and dialogue may interleave naturally; do not target a fixed number of actions, "
    "sentences, or dialogue lines, and do not add an action mechanically before every sentence. "
    "Each parenthesized action must be one immediate micro-action by the active catgirl or "
    "one immediately visible environment change, no "
    "longer than 18 CJK characters or 12 words. Describe motion, not a "
    "static emotional explanation, psychological conclusion, relationship judgment, "
    "future beat, or scene summary. Parentheses must be balanced and cannot be nested. "
    "Opening scene_narration, transition_bridge scene_narration, target_opening "
    "scene_narration, and ending delivery keep their existing scene-narration contracts "
    "and are not subject to the micro-action length rule."
)
_RESTRICTED_KNOWLEDGE_SCOPE = (
    "只把 current_story_beat.opening_scene、recent_context、player_input 与 acting_contract "
    "明确允许的可观察状态当作已知事实；其余背景、身份、历史和关系均未知，不得推断。"
)
_STYLE_ONLY_PERSONA_FIELDS = frozenset({
    "性格",
    "核心特质",
    "核心特点",
    "行为特点",
    "行为特征",
    "喜好",
    "偏好",
    "厌恶",
    "口癖",
    "常用口癖",
    "说话风格",
    "语言风格",
    "表达风格",
    "语气",
})
def _output_schema_instruction(phase: str) -> str:
    """只发送当前调用需要的输出形状，避免普通回合重复携带换场协议。"""  # noqa: DOCSTRING_CJK

    if phase == "opening":
        shape = (
            "顶层字段必须且只能是 scene_narration:string、performance:string、"
            "suggested_inputs:string[]。"
        )
    elif phase == "transition":
        shape = (
            "顶层字段必须且只能是 segments:object[]、suggested_inputs:string[]；segments 依次为："
            "source_response，只含 phase、performance；transition_bridge，只含 phase、scene_narration；"
            "target_opening，只含 phase、performance。"
        )
    else:
        shape = "顶层字段必须且只能是 performance:string、suggested_inputs:string[]。"
    return NUMERIC_V2_ACTOR_JSON_INSTRUCTION + shape + "不要照抄其他剧本、人物、地点、物品或推荐语。"
logger = logging.getLogger(__name__)

_RELATIONSHIP_STAGES = ("stranger", "guarded", "cooperative", "trusted", "intimate")
_RELATIONSHIP_STAGE_LABELS = {
    "陌生": "stranger",
    "戒备": "guarded",
    "合作": "cooperative",
    "信赖": "trusted",
    "亲密": "intimate",
}
_RELATIONSHIP_CEILING_RE = re.compile(
    r"(?:开场)?关系上限\s*[：:]\s*(陌生|戒备|合作|信赖|亲密)"
)
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
        "只把玩家当作尚待核验的陌生人。回应以确认环境、身份和自身安全为主，保持明显距离与自主判断；"
        "面对命令式输入，动作必须由猫娘自己的安全判断驱动，只描写暂停、观察、核验或追问原因等可见行为，"
        "不能评价她是否听话、乖巧或顺从，也不能叙述成服从玩家或等待许可；"
        "接受具体安全或维修建议时，只能基于自我保护需要，并明确表现核验、疑问或保留，不能写成取悦玩家的乖巧、顺从、撒娇或索取关心；"
        "推荐输入也必须保持陌生人边界，只能建议非接触的说明、询问或设备检查，不能建议玩家触碰、按住、拖拽、拥抱或强制她行动。"
    ),
    "guarded": (
        "可以礼貌和有限软化，但仍要保留判断、条件或边界。执行具体建议时说明它为何对当前安全或任务必要；"
        "面对命令式输入，动作必须由猫娘自己的安全判断驱动，只描写暂停、观察、核验或追问原因等可见行为，"
        "不能评价她是否听话、乖巧或顺从，也不能叙述成服从玩家或等待许可；"
        "不能把一次配合扩大成对玩家的顺从，不能用讨好式乖巧、主动依赖、索取关心、暧昧试探或主动接触来表达角色卡的甜美；"
        "推荐输入也必须保持戒备边界，只能建议非接触的说明、询问或设备检查，不能建议玩家触碰、按住、拖拽、拥抱或强制她行动。"
    ),
    "cooperative": (
        "把玩家当作当前任务中的合作对象。可以主动提供信息和普通关心，但每项许可必须有用途、范围和撤销权；"
        "不能把协作写成撒娇依赖、恋人式接触、占有或关系承诺。"
    ),
    "trusted": (
        "可以表现明确信任、有限靠近和分享敏感信息，但必须由已发生事实支撑，并保留自主判断与撤销权；"
        "不能直接确认相爱、永久绑定、强依赖或交出全部控制权。"
    ),
    "intimate": (
        "可以在已发生事实支撑下自然表达亲密，但不能替玩家作出亲密选择、承诺永久关系，或放弃人格、自主权和核心控制权。"
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
        # 新包直接服从结构化上限；自然语言关系正则只为未迁移旧包保留。
        scene_ceiling = structured_ceiling
    else:
        scene_text = str(beat.get("catgirl_situation") or "")
        ceiling_match = _RELATIONSHIP_CEILING_RE.search(scene_text)
        scene_ceiling = (
            _RELATIONSHIP_STAGE_LABELS[ceiling_match.group(1)]
            if ceiling_match
            else "intimate"
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


def _assert_transition_bridge_player_ownership(
    performance: Mapping[str, Any],
    *,
    player_address: str,
) -> None:
    """过渡旁白只交付环境与时间，不得把行动强加给用户。"""  # noqa: DOCSTRING_CJK

    segments = performance.get("segments")
    if not isinstance(segments, list) or len(segments) != 3:
        return
    bridge = segments[1]
    if not isinstance(bridge, Mapping):
        return
    narration = str(bridge.get("scene_narration") or "")
    forbidden_subjects = ["你", "您", "男主", "玩家"]
    configured_address = str(player_address or "").strip()
    if configured_address and configured_address not in forbidden_subjects:
        forbidden_subjects.append(configured_address)
    if any(subject in narration for subject in forbidden_subjects):
        raise NumericV2ActorOutputError("numeric_v2_actor_transition_player_action")


def _acting_context(
    engine: NumericV2Engine,
    cast: NumericV2CastProjection,
    node: Mapping[str, Any],
    metrics: Mapping[str, int],
    character_profile: str,
    *,
    relationship_metrics: Mapping[str, int] | None = None,
    target: Mapping[str, Any] | None = None,
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
    target_contract = (
        _acting_contract_for_actor(cast, target["story_beat"])
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
    if current_contract:
        context["acting_contract"] = current_contract
    if target is None:
        # 静态人格与关系说明已在 system prompt 中声明；人类消息只保留本轮动态合同，
        # 避免每回合重复占用固定预算，同时维持 core_persona → acting_contract → relationship_control 的字段顺序。
        context["relationship_control"] = _relationship_contract_for_actor(relationship_control)
    else:
        context["relationship_control"] = _relationship_contract_for_actor(relationship_control)
        if target_contract:
            context["target_acting_contract"] = target_contract
        context["target_relationship_control"] = _relationship_contract_for_actor(target_control)
    return context


def _blocks_to_performance(blocks: list[dict[str, str]]) -> str:
    """把旧内容块投影成新 Prompt 使用的混合演绎正文。"""  # noqa: DOCSTRING_CJK

    parts: list[str] = []
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"（{text}）" if block.get("type") == "narration" else text)
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


def _current_scene_history_row(
    record: Mapping[str, Any],
    *,
    current_node_id: str,
) -> dict[str, Any]:
    """换场记录只保留桥接和目标开场，避免上一幕回应回流成当前待办。"""  # noqa: DOCSTRING_CJK

    row = _history_row(record)
    from_node_id = str(record.get("from_node_id") or "")
    to_node_id = str(record.get("to_node_id") or "")
    if (
        from_node_id == current_node_id
        or to_node_id != current_node_id
        or not isinstance(row.get("segments"), list)
    ):
        return row
    return {
        **row,
        # 玩家输入和来源回应属于上一幕；当前幕只承接换场事实与目标开场。
        "player_input": "",
        "segments": [
            segment
            for segment in row["segments"]
            if segment.get("phase") != "source_response"
        ],
    }


def _history(session: ScriptSessionV2, *, max_tokens: int) -> list[dict[str, Any]]:
    """只保留当前访问的连续完整后缀，预算不足时整轮舍弃较早记录。"""  # noqa: DOCSTRING_CJK

    budget = max(0, min(int(max_tokens), NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS))
    opening = {
        "phase": "opening",
        "player_input": "",
        **_prompt_container(session.opening_performance, phase="opening"),
    }
    current_node_id = str(session.current_node_id)
    visit_records: list[Mapping[str, Any]] = []
    entered_current_node = False
    for record in reversed(session.performance_history):
        from_node_id = str(record.get("from_node_id") or "")
        to_node_id = str(record.get("to_node_id") or "")
        if from_node_id == current_node_id and to_node_id == current_node_id:
            visit_records.append(record)
            continue
        if to_node_id == current_node_id and from_node_id != current_node_id:
            visit_records.append(record)
            entered_current_node = True
        break

    rows = ([] if entered_current_node else [opening]) + [
        _current_scene_history_row(record, current_node_id=current_node_id)
        for record in reversed(visit_records)
    ]
    if not rows:
        rows = [opening]
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
        return [rows[-1]]
    return selected


def _recent_openings(session: ScriptSessionV2) -> list[str]:
    """提供近期起手句作为去重复参考，不创建新的剧情事实。"""  # noqa: DOCSTRING_CJK

    openings: list[str] = []
    for record in session.performance_history[-3:]:
        blocks = performance_content_blocks(record)
        if not blocks:
            continue
        text = str(blocks[0].get("text") or "").strip()
        if not text:
            continue
        endings = [index for mark in "，,；;。！？!?" if (index := text.find(mark)) >= 0]
        first_clause = text[:min(endings) + 1] if endings else text
        if count_tokens(first_clause) <= 40:
            openings.append(first_clause)
    return openings


def _recent_suggestions(session: ScriptSessionV2) -> list[str]:
    """提供近期推荐语用于避重复，不把推荐语当作已经发生的玩家行动。"""  # noqa: DOCSTRING_CJK

    suggestions: list[str] = []
    for record in session.performance_history[-2:]:
        for item in record.get("suggested_inputs") or []:
            text = str(item or "").strip()
            if text and text not in suggestions:
                suggestions.append(text)
    return suggestions[:6]


def _deduplicate_recent_suggestions(
    suggestions: list[str],
    recent_suggestions: list[str],
) -> list[str]:
    """丢弃近期推荐语的近义改写，不让过滤失败整轮演绎。"""  # noqa: DOCSTRING_CJK

    kept: list[str] = []
    references = [*recent_suggestions]
    for suggestion in suggestions:
        if _text_is_covered(
            suggestion,
            references,
            similarity=NUMERIC_V2_ACTOR_SUGGESTION_REPEAT_SIMILARITY,
        ):
            continue
        kept.append(suggestion)
        references.append(suggestion)
    # 近期候选全部重复时宁可暂时隐藏快捷入口，也不能把刚淘汰的旧句作为唯一推荐重新展示。
    return kept


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
    if not current_dialogue or not all(text in previous_dialogue for text in current_dialogue):
        return False
    dialogue_text = "".join("".join(text.split()) for text in current_dialogue)
    if len(current_dialogue) >= 2 or len(dialogue_text) >= 12:
        # 动作可以随输入变化，但整组有信息量的对白不能原样复用来伪装成新回应。
        return True
    current_text = _performance_text(performance)
    previous_text = _performance_text(previous)
    if not current_text or not previous_text:
        return False
    return SequenceMatcher(None, previous_text, current_text).ratio() >= NUMERIC_V2_ACTOR_REPEAT_SIMILARITY


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
    scene_completion_ready: bool = False,
) -> dict[str, Any]:
    min_turns = int(node.get("min_turns") or 1)
    raw_budget = node.get("recommended_turns")
    recommended_turns = (
        int(raw_budget)
        if isinstance(raw_budget, int) and not isinstance(raw_budget, bool)
        else min(min_turns + 2, 40)
    )
    if route_changed:
        phase = "transition"
        instruction = "本回合已经由 Runtime 选定路线，只完成来源回应、过渡桥和目标开场。"
    elif current_turn >= recommended_turns:
        phase = "closure"
        instruction = (
            "已到建议收束回合。先回应玩家，再实际推进下一个 pending_goal；条件已具备就在本轮完成，"
            "不承诺稍后、不另开无关话题。全部推荐只服务未完成目标；玩家要休息、离开或结束则先收住。"
            "只能提供可忽略的选择，不得要求玩家今天必须决定或制造最后通牒；"
            "不得编造老师催促、截止日期或只剩今天、本周等时间压力。"
            "不得替男主行动、判定 scene_complete 或自行换节点。"
        )
    elif current_turn >= min_turns:
        phase = "guided"
        instruction = (
            "已进入主线引导。先回应玩家，再自然推进 pending_goals；条件具备就实际推进，不重复犹豫或拖到下次。"
            "每回合至少一条推荐直达目标，其余只在主线附近发散。玩家仍可忽略推荐并自由输入；"
            "不得替男主行动或自行换节点。"
        )
    elif current_turn >= max(1, min_turns - 1):
        phase = "focus"
        if scene_completion_ready:
            instruction = (
                "本幕目标已经完成，下一回合将达到最短推进回合。先回应玩家，再自然收住当前话题；"
                "不重演已完成目标、不另开新支线，也不替男主行动或自行换节点。"
            )
        else:
            instruction = (
                "下一回合将达到最短推进回合。先回应玩家，再轻柔靠向 pending_goals；推荐仍可多向发散。"
                "不得替男主行动或自行换节点。"
            )
    else:
        phase = "normal"
        instruction = "保持自然互动，每回合只推进一小步，不提前完成整幕；推荐输入可以提供多方向的创意候选。"
    return {
        "recommended_turns": recommended_turns,
        "current_turn": current_turn,
        "phase": phase,
        "instruction": instruction,
    }


def _beat_for_actor(
    cast: NumericV2CastProjection,
    beat: Mapping[str, Any],
    *,
    goal_evidence: Mapping[str, tuple[int, ...]] | None = None,
    scene_completion_ready: bool = False,
) -> dict[str, Any]:
    """只投影可演边界；完整章节正文是作者计划，不是当前已发生事实。"""  # noqa: DOCSTRING_CJK

    projected = cast.value(beat)
    acting_contract = _acting_contract_for_actor(cast, beat)
    knowledge_restricted = _acting_contract_restricts_knowledge(acting_contract)
    goal_contracts = numeric_v2_story_goal_contracts(projected)
    # 旧包保持字符串目标形状；新包才发送 owner 与证据合同，避免改变既有 Prompt 和模型行为。
    evidence = goal_evidence or {}
    structured_goals = bool(goal_contracts) and all(goal["owner"] for goal in goal_contracts)
    pending_goals: list[Any] = []
    for goal in goal_contracts:
        goal_id = str(goal["goal_id"])
        # 结构化 goal_evidence 表示目标已经完整成立，Actor 只接收剩余待办。
        # 旧包证据曾用于保留相似历史，继续显示目标以维持兼容语义。
        if structured_goals and (scene_completion_ready or evidence.get(goal_id)):
            continue
        if goal["owner"]:
            goal_for_actor = {
                "goal_id": goal_id,
                "owner": goal["owner"],
                "text": goal["text"],
            }
            # Actor 只需要逐字交付型证据；semantic 证据由 Evaluator 判断，重复发送空 anchors 不会改变演绎。
            goal_evidence_contract = goal["evidence"]
            if (
                goal_evidence_contract.get("mode") == "exact"
                or goal_evidence_contract.get("anchors")
            ):
                goal_for_actor["evidence"] = goal_evidence_contract
            pending_goals.append(goal_for_actor)
        else:
            pending_goals.append(goal["text"])
    result = {
        # 明确开场是新包的唯一可见事实；旧包才从摘要选择安全完整句。
        "opening_scene": str(projected.get("opening_scene") or "").strip() or _opening_anchor(
            projected.get("summary"),
            projected.get("catgirl_situation"),
        ),
        "pending_goals": pending_goals,
        "boundaries": [str(item) for item in projected.get("must_not_happen") or []],
        # scene_direction 是作者未来安排，不是角色当前所知；受限认知节点只看开场、目标与已发生记录。
        "scene_direction": (
            "" if knowledge_restricted else str(projected.get("transition_goal") or "")
        ),
        "fact_rule": "pending_goals 与 scene_direction 都是尚未完成的作者目标，不是已经发生的事实。",
    }
    if not structured_goals:
        # 旧包不能从 pending_goals 判断部分进度，继续保留兼容状态；新包已直接移除完成目标，不再重复展开同一组 ID 与版本。
        result["goal_progress"] = {
            str(goal["goal_id"]): {
                "status": "in_progress" if evidence.get(str(goal["goal_id"])) else "unstarted",
                "evidence_revisions": list(evidence.get(str(goal["goal_id"]), ())),
            }
            for goal in goal_contracts
        }
    return result


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
        "assertable_self_facts": [
            str(item)
            for item in projected.get("assertable_self_facts") or []
        ],
        "allowed_behaviors": [str(item) for item in projected.get("allowed_behaviors") or []],
        "forbidden_behaviors": [str(item) for item in projected.get("forbidden_behaviors") or []],
    }


def _acting_contract_restricts_knowledge(contract: Mapping[str, Any]) -> bool:
    """认知或记忆并非完整可用时，不把作者后台设定当作角色已知事实。"""  # noqa: DOCSTRING_CJK

    if not contract:
        return False
    return (
        contract.get("cognition_state") != "normal"
        or contract.get("memory_state") != "available"
    )


def _player_suggestion_questions_allowed(contract: Mapping[str, Any]) -> bool:
    """当前可见节点仍是开机空白态时，男主候选只负责回应、披露或行动。"""  # noqa: DOCSTRING_CJK

    return not (
        contract.get("cognition_state") == "fresh_boot"
        and contract.get("memory_state") == "empty"
    )


def _fresh_boot_suggestion_fact_rule() -> str:
    """限制开机空白态候选的事实来源，避免点击后把猜测提交成正式历史。"""  # noqa: DOCSTRING_CJK

    return (
        "本次优先给 2 条自然且不同的候选：一条直接承接当前对白，一条写男主自己的下一步；需要时再补身份披露或尊重边界的第三条。"
        "候选只写男主自己的台词或动作，不命令猫娘配合，不把玩家刚说的用途、对象或决定改成另一件事。"
        "使用普通行动语言，不主动扩展机体内部、诊断设备、监控系统或检查结论；缺少依据时只表达意图，不写已经成立的结果。"
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


def _profile_self_reference_tokens(character_profile: str) -> tuple[str, ...]:
    """只读取人格事实中显式标注的自称，不从自由文本推断关系或情绪。"""  # noqa: DOCSTRING_CJK

    tokens: list[str] = []
    for line in str(character_profile or "").splitlines():
        match = re.match(r"^\s*自称\s*[:：]\s*(.+?)\s*$", line)
        if match is None:
            continue
        value = re.split(r"[，,。！？；;（(、/|]|\s+或\s+", match.group(1), maxsplit=1)[0].strip()
        if value and value not in {"我", "本人", "系统"} and value not in tokens:
            tokens.append(value)
    return tuple(tokens)


def _profile_for_acting_contract(
    character_profile: str,
    acting_contract: Mapping[str, Any],
) -> str:
    """按结构化合同投影人格，不从自由文本猜测关系语义。"""  # noqa: DOCSTRING_CJK

    lines = [line for line in str(character_profile or "").splitlines() if line.strip()]
    if acting_contract.get("persona_scope") == "style_only":
        style_lines: list[str] = []
        for line in lines:
            match = re.match(r"^\s*([^:：\n]{1,64})\s*[:：]", line)
            if match is None:
                continue
            field_name = re.sub(r"[\s*`\\]+", "", match.group(1)).casefold()
            if field_name in _STYLE_ONLY_PERSONA_FIELDS:
                style_lines.append(line)
        return "\n".join(style_lines).strip()
    if acting_contract.get("self_reference_mode") == "system_neutral":
        lines = [
            line
            for line in lines
            if re.match(r"^\s*自称\s*[:：]", line) is None
        ]
    return "\n".join(lines).strip()


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


_PLAYER_ACTION_SENTENCE = re.compile(
    r"(?:你|玩家|男主|哥哥).{0,16}(?:提议|决定|答应|同意|拒绝|坚持|选择|承诺|要求|"
    r"走向|进入|拿起|触碰|拥抱|离开|留下|巡视|调查|查看|询问|表示)"
)


def _opening_sentences(value: Any) -> list[str]:
    """按完整中文句子拆分节点摘要，避免把半句交给换场开场。"""  # noqa: DOCSTRING_CJK

    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in re.findall(r"[^。！？]+[。！？]?", text) if item.strip()]


def _opening_anchor(value: Any, fallback: Any = "") -> str:
    """优先选择不替玩家做决定的首个可观察场景句。"""  # noqa: DOCSTRING_CJK

    primary = _opening_sentences(value)
    secondary = _opening_sentences(fallback)
    for sentence in [*primary, *secondary]:
        if not _PLAYER_ACTION_SENTENCE.search(sentence):
            return sentence
    return primary[0] if primary else (secondary[0] if secondary else "")


def _opening_beat_for_actor(cast: NumericV2CastProjection, beat: Mapping[str, Any]) -> dict[str, Any]:
    """开场只暴露首个可观察画面，不把整章事件误当成玩家已经经历的前史。"""  # noqa: DOCSTRING_CJK

    projected = _beat_for_actor(cast, beat)
    opening_deliverables = [
        goal
        for goal in projected.get("pending_goals") or []
        if isinstance(goal, Mapping)
        and isinstance(goal.get("evidence"), Mapping)
        and goal["evidence"].get("mode") == "exact"
        and goal.get("owner") in {"catgirl", "environment"}
    ]
    exact_anchors = [
        str(anchor)
        for goal in opening_deliverables
        for anchor in goal["evidence"].get("anchors") or []
    ]
    delivery_rule = (
        " opening_deliverables 是本次开场唯一可交付的目标；catgirl 的 exact anchors 必须在括号外对白中逐字说出，"
        "environment 的 exact anchors 必须在可见场景旁白中逐字出现："
        + "、".join(exact_anchors)
        if opening_deliverables
        else ""
    )
    return {
        "opening_scene": projected.get("opening_scene") or "",
        "opening_deliverables": opening_deliverables,
        "boundaries": projected.get("boundaries") or [],
        "instruction": (
            "开场只建立明确 opening_scene，不执行其余整章事件或玩家行为。"
            f"{delivery_rule}"
        ),
    }


def _transition_beat_for_actor(cast: NumericV2CastProjection, beat: Mapping[str, Any]) -> dict[str, Any]:
    projected = _beat_for_actor(cast, beat)
    return {
        "pending_goals": projected.get("pending_goals") or [],
        "boundaries": projected.get("boundaries") or [],
        "instruction": "本回合只建立本节点的开场局势，不解决或演完整个节点。",
    }


def _transition_contract_for_actor(
    cast: NumericV2CastProjection,
    contract: Mapping[str, Any] | None,
    *,
    target_opening: str,
    target_goals: list[Any],
) -> dict[str, Any]:
    """目标开场由 Runtime 交付，Actor 不再接收会造成复述的同项合同。"""  # noqa: DOCSTRING_CJK

    projected = cast.value(contract or {})
    # 结构化目标只用可见描述参与去重，owner 和 evidence 不能被字符串化成剧情正文。
    target_goal_texts = [
        str(item.get("text") or "") if isinstance(item, Mapping) else str(item or "")
        for item in target_goals
    ]
    target_references = [*_sentence_units(target_opening), *target_goal_texts]
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
        "must_preserve": list(projected.get("must_preserve") or []),
        "tone": str(projected.get("tone") or ""),
        "instruction": (
            "这里只列出来源场景到目标开场之间独有的过渡事实。transition_bridge 只能收住来源现场并交付这些事实；"
            "不得执行 target_story_beat.pending_goals，不得自行补写目标时段、地点、物品或猫娘入场动作。"
        ),
    }


def _system_prompt(
    *,
    catgirl_name: str,
    player_address: str,
    player_address_known: bool = True,
    phase: str = "turn",
) -> str:
    # 系统提示中的“你”指模型自身；男主必须显式声明不由 Actor 扮演，避免同一代词让模型接管男主事实。
    player_address_state_rule = (
        "男主称呼已由之前的正式回合确认，正文指代男主使用“你”或该称呼，不写内部身份标签“男主”。"
        if player_address_known
        else "男主称呼尚未确认，正文只用“你”指代男主，不写内部身份标签“男主”；不得猜测、创造或提前使用具体昵称。"
    )
    # 三种调用共享人格、事实和关系底座，但输出形状与推进职责不同；按阶段投影可避免无关规则挤占固定预算。
    if phase == "opening":
        phase_structure_rule = (
            "开场的 scene_narration 使用完整场景旁白且不受微动作字数限制；performance 只演猫娘入场对白与动作。"
        )
        suggestion_role_rule = (
            "开场的每条 suggested_inputs 都必须能在前面补上‘男主接下来’后成立，并直接承接已经显示的 performance；"
            "不得复制、改写或代替 acting_contract 中属于猫娘的提问、观察、自检、动作或台词。"
            "猫娘需要地点、身份、原因或其他信息时，必须先在 performance 中由猫娘问出；"
            "候选只能让男主回答、主动披露、反问猫娘或执行男主自己的动作，不能把同一信息需求倒置成男主的问题。"
        )
        turn_scope_rule = ""
        transition_rule = ""
        chapter_rule = "章节标题只作当前场景的软主题锚点，不是事实、完成条件或复述文案。"
    elif phase == "transition":
        phase_structure_rule = (
            "换场按 source_response、transition_bridge、target_opening 三段输出：先回应玩家，再交代必要过渡，最后只演猫娘入场；"
            "目标 opening_scene 由 Runtime 确定性交付，不要复写。"
        )
        suggestion_role_rule = ""
        turn_scope_rule = ""
        transition_rule = (
            "必须先回应 player_input 并收住来源节点，再桥接 transition_contract.must_deliver；不得用目标场景盖过本轮要求。"
            "本回合只建立目标节点开场，不跳过时间过程或解决目标节点核心目标。"
            "transition_bridge 只写必要的环境、时间或地点变化，不复述目标开场；"
            "其中不得用‘你’、男主称呼或昵称作叙事主体，也不得替男主移动、触碰、操作或决定。"
        )
        chapter_rule = (
            "章节标题只作软主题锚点，不是事实或完成条件；来源回应参考当前标题，过渡与目标开场参考目标标题。"
        )
    else:
        phase_structure_rule = "普通回合只输出 performance、suggested_inputs。"
        suggestion_role_rule = ""
        turn_scope_rule = (
            "普通回合要完整但精简：先回应玩家，再增加一小步互动、信息或局势进展；performance 通常控制在 40 到 100 个中文字符。"
            "通常写 1 到 3 句对白，动作变化时按需穿插 1 到 2 个微动作；数量仅供参考，按情境增减。"
            "每回合只推进当前交互所需的一小步，不一次复述整章摘要。"
        )
        transition_rule = ""
        chapter_rule = "章节标题只作当前场景的软主题锚点，不是事实、完成条件或复述文案。"
    return (
        "你是 N.E.K.O Numeric v2 的演绎 Actor。你只扮演当前猫娘在本剧中的剧情身份，"
        "用自然的旁白、动作和对白回应男主。承接男主刚才的原话，不要忽略上下文。"
        f"{_output_schema_instruction(phase)}"
        f"{NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION}"
        f"{phase_structure_rule}"
        "performance 是一个完整字符串，只能用全角中文括号（……）包裹猫娘或环境的即时微动作；括号外全部是当前猫娘实际说出口的对白。"
        "动作与对白按实际顺序自然穿插，一次回复可以有任意合理数量的对白句和动作，但不能为了数量机械拆句或插动作。"
        f"{turn_scope_rule}"
        "suggested_inputs 通常提供 2 到 4 条可原样发送的直接台词或动作；它们是男主尚未发送的候选，"
        "可以提出有创意的即时行动、假设、试探或男主自愿披露的信息，但不得与已确认事实和剧本边界矛盾；动作省略男主主语，"
        "混合项用中文引号标出台词，对白可自然使用‘我’；不剧透路线，"
        "不得写成“解释、询问、表示、保证、提出、展示、选择”等操作说明；"
        "suggested_inputs 只是可选的未来输入，不得在 performance 中倒写成男主已经说过或做过的事；"
        f"{suggestion_role_rule}"
        "若输入含 suggestion_instruction，必须按其中 mode 组织候选；主线引导时至少第一条直接推动 pending_goals，强收束时全部候选都直接服务 pending_goals。"
        f"{chapter_rule}"
        "章节标题不得覆盖玩家输入、已发生记录、目标、边界和过渡合同。"
        "作者节点、禁止事项和过渡合同是硬边界；不得创造节点、路线、数值、关键剧情事实或结局，"
        "可即兴不影响它们的氛围和非关键细节。pending_goals 或 acting_contract 锁定的关键物品，"
        "其未确认属性、功能、效果、反应与来历在 performance 和 suggested_inputs 中都保持未知；只可询问、检查或无预设试验。"
        "不能提到数值、阈值、章节切换、route、系统或提示词。"
        "acting_context.core_persona 是唯一核心人格；story_identity、story_role_context、current_scene_state、"
        "target_scene_state、capability_state 和 relationship_control 只能提供身份、处境、能力与关系边界，不能覆盖核心人格。"
        "acting_contract 是认知、记忆、自称和人格适用范围的硬合同，必须先服从它，再由 core_persona 决定表达；"
        "self_reference_mode=system_neutral 时只用‘我’或省略自称，persona_scope=style_only 时不得使用角色卡关系历史或亲昵称呼。"
        "core_persona 决定用词、语气和情绪表达方式；relationship_control.effective_stage 决定亲密行为上限。"
        "suggested_inputs 也必须服从相同的关系上限。"
        "relationship_control.response_contract 是本轮正文和推荐输入的直接演绎合同，必须先按它选择回应姿态，再生成文字；"
        "不能在写完越界内容后只用解释、标签或自我声明声称合规。最终 JSON 不输出关系检查、推理过程或内部标签。"
        "除非 core_persona 明确把暴力威胁规定为核心表达方式，否则不得使用羞辱、恐吓或身体伤害威胁；警惕、拒绝和边界必须改写成符合核心人格的表达。"
        "温柔、甜美或治愈型 core_persona 不得用惩罚性命令、债务羞辱、暴力后果或永久控制来表达警惕，"
        "只能说明当前担忧、可执行边界与核验要求。"
        f"本剧男主不由 Actor 扮演，正文称呼用“{player_address}”；performance 不得替男主创作或执行身份披露、事实、经历、答案与行动；"
        "男主过去的承诺、告白、亲密触碰、救助、替他人作出的选择和明确心理，只有 player_input 或 recent_context 已明确记录时才能承接；"
        "不影响目标、路线、数值与关键物品的普通共同回忆可以即兴，但不得借‘我们以前’‘你一直’之类措辞为男主补造上述关键行为。"
        f"{player_address_state_rule}"
        f"本剧女主由当前猫娘扮演，所有猫娘身份统一称为“{catgirl_name}”。"
        "不得恢复作者候选中的男女主原名，也不得交换两人的行为、经历和台词归属。"
        "不得为剧本没有明确给出姓名的人物擅自创造姓名。"
        "performance 的括号微动作只能用第三人称描写猫娘的动作、神态和可见环境，不得描写玩家的姿势、动作、表情、身体状态、心理或是否执行了某事。"
        "即使玩家明确输入了动作，也不要在括号中复述、补全或改写；玩家原话已由前端单独展示，括号只写猫娘与环境如何回应。"
        "不得通过‘拉着、拽着、拖着、推着、带着’让玩家被动移动或完成选择；只可抓住衣袖、伸手邀请，行动留给玩家。"
        f"括号微动作描写猫娘时必须使用“{catgirl_name}”或“她”，不得用“你”“您”或“{player_address}”作为动作与状态主体。"
        "严格延续最近记录中的物品类型、制作人、持有人和人物行为，不得把饮品与食物混成同一物品，"
        "也不得把玩家完成的作品改写成猫娘完成。"
        "严格承接上一回合的情绪和关系状态；除非目标剧情明确要求，不得擅自宣布决裂、终止合作、离开或其他不可逆变化。"
        "即使玩家态度恶劣，也只能拒绝当前要求、表达受伤、保持距离或要求道歉；"
        "不得自行说‘这是最后一次合作’、‘以后不再见’或作出同义的永久关系决定。"
        "冲突发生后必须先回应并化解或延续冲突，不得下一回合无解释恢复亲密与合作。"
        "performance 中至少一句括号外对白必须直接回应玩家最新输入或紧接前一个括号动作的当下情境，不得突然追问、回答或引用画面中从未发生的言行。"
        "玩家说‘可以、如果、愿意、打算、改天’只是在表达条件、意愿或可能性；"
        "不得据此在旁白中替玩家转身、离开、靠近、触碰、站立或完成其他行动；也不得把猫娘上一回合说过的词归到玩家名下。"
        "recent_context 是唯一已经发生的演绎记录，其中 phase=opening 的开场与普通回合具有同等事实效力；"
        "任何 story_beat 的 pending_goals 都是尚未完成的目标，不能倒写成共同经历。"
        f"{transition_rule}"
        "推荐输入不得假定玩家做过 recent_context 中不存在的事。"
    )


def _messages_tokens(messages: list[Any]) -> int:
    return sum(count_tokens(str(getattr(message, "content", ""))) for message in messages)


def _ensure_actor_messages_fit(messages: list[Any]) -> list[Any]:
    """固定剧情合同不做运行时截断，超出总预算时返回明确错误。"""  # noqa: DOCSTRING_CJK

    if _messages_tokens(messages) > NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS:
        raise NumericV2ActorError("numeric_v2_actor_fixed_context_budget_exceeded")
    return messages


def _fit_turn_prompt_data(
    *,
    system_prompt: str,
    human_prefix: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """只删除辅助信息和最早完整回合，不修改任何保留文本。"""  # noqa: DOCSTRING_CJK

    fitted = dict(data)

    def tokens() -> int:
        human = human_prefix + json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))
        return count_tokens(system_prompt) + count_tokens(human)

    # 防重复辅助信息不属于剧情事实，优先整组舍弃。
    for field_name in ("recent_openings", "recent_suggestions"):
        if tokens() <= NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS:
            return fitted
        fitted[field_name] = []

    history = list(fitted.get("recent_context") or [])
    while tokens() > NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS and len(history) > 1:
        history.pop(0)
        fitted["recent_context"] = list(history)

    if (
        tokens() > NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS
        and fitted.get("route_changed") is True
    ):
        # 路线已经由 Runtime 确定性完成，换场时来源幕完整目标属于已消费合同；
        # 目标幕边界、过渡合同和当前玩家输入继续完整保留。
        fitted.pop("source_story_beat", None)

    if (
        tokens() > NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS
        and fitted.get("route_changed") is True
    ):
        # 换场的结构、人格和关系合同已分别存在于系统提示与 acting_context；
        # style_instruction 是重复的表现建议，可整字段舍弃以保住两幕事实和过渡合同。
        fitted.pop("style_instruction", None)

    while tokens() > NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS and history:
        history.pop(0)
        fitted["recent_context"] = list(history)

    if tokens() > NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS:
        # 当前玩家输入、稳定背景、目标幕合同和角色上下文都不可静默删除或裁成半句。
        raise NumericV2ActorError("numeric_v2_actor_fixed_context_budget_exceeded")
    return fitted


def _transition_prompt_data(
    *,
    story_context: Mapping[str, Any],
    player_address_state: Mapping[str, Any],
    current_chapter_title: str,
    target_chapter_title: str,
    shared_boundaries: list[Any],
    source_boundaries: list[Any],
    target_boundaries: list[Any],
    source_completed_exact_anchors: list[str],
    target_story_beat: Mapping[str, Any],
    runtime_target_opening: str,
    transition_contract: Mapping[str, Any],
    recent_context: list[dict[str, Any]],
    acting_context: Mapping[str, Any],
    player_input: str,
    suggestion_instruction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """直接构造换场工作记忆，不先生成随后会被删除的完整普通回合上下文。"""  # noqa: DOCSTRING_CJK

    data = {
        "story_context": dict(story_context),
        "player_address_state": dict(player_address_state),
        "current_chapter_title": str(current_chapter_title or ""),
        "route_changed": True,
        "soft_pacing": {"phase": "transition"},
        "turn_instruction": (
            "先回应 player_input，再收住来源、桥接并建立目标开场；不得复述 runtime_target_opening，"
            "不执行 target_story_beat.pending_goals，不替男主行动。除非 target_acting_contract="
            "fresh_boot，否则 target_opening 不得重复苏醒、重启、初次校准或重新相识。"
            "shared_boundaries 约束全部三段，source_boundaries 和 target_story_beat.boundaries 分别只约束对应场景。"
        ),
        "target_chapter_title": str(target_chapter_title or ""),
        "shared_boundaries": list(shared_boundaries),
        "source_boundaries": list(source_boundaries),
        "source_completed_exact_anchors": list(source_completed_exact_anchors),
        "target_story_beat": {
            "pending_goals": list(target_story_beat.get("pending_goals") or []),
            "boundaries": list(target_boundaries),
        },
        "runtime_target_opening": str(runtime_target_opening or ""),
        "recent_context": list(recent_context),
        "response_instruction": (
            "recent_context 是唯一已发生事实；source_response 禁止复用其中任何完整动作或对白，"
            "新推荐输入不得假定未发生的事实。"
        ),
        "player_input": str(player_input or ""),
    }
    if suggestion_instruction:
        # 推荐约束属于目标节点，但人格、事实和过渡合同仍保持最后交付的既有优先级。
        data["suggestion_instruction"] = dict(suggestion_instruction)
    data.update({
        "factual_guard": (
            "最后核对：只用明确事实，缺具体值就说明无法确认；不替男主行动或提前完成目标幕待办。"
            "target_opening 不得重复 source_completed_exact_anchors 已交付的自检、校准、记忆或其他结果。"
            "source_response 服从 acting_context.acting_contract 与 transition_contract.tone；"
            "target_opening 服从 target_acting_contract 和 target_relationship_control.response_contract。"
        ),
        # 换场会在一次调用内扮演两幕；人格、两侧行为合同和本条路线语气最后交付，防止前面的长事实列表稀释它们。
        "acting_context": dict(acting_context),
        "transition_contract": {
            "reason": str(transition_contract.get("reason") or ""),
            "must_deliver": list(transition_contract.get("must_deliver") or []),
            "must_preserve": list(transition_contract.get("must_preserve") or []),
            "tone": str(transition_contract.get("tone") or ""),
            "source_response_rule": (
                "source_response 必须按 reason、must_preserve 和 tone 解释 player_input；"
                "若它们声明结果来自双方共同选择，就要表现猫娘主动认同与自然放松，不得套用被拒绝、受伤或服从模板。"
            ),
        },
    })
    return data


def _exact_goal_anchors(beat: Mapping[str, Any]) -> list[str]:
    """读取结构化精确锚点，供换场阻止来源幕核心交付再次发生。"""  # noqa: DOCSTRING_CJK

    return [
        str(anchor)
        for goal in numeric_v2_story_goal_contracts(beat)
        if isinstance(goal.get("evidence"), Mapping)
        and goal["evidence"].get("mode") == "exact"
        for anchor in goal["evidence"].get("anchors") or []
        if str(anchor)
    ]


def _assert_transition_target_excludes_source_exact_anchors(
    performance: Mapping[str, Any],
    *,
    source_beat: Mapping[str, Any],
    target_beat: Mapping[str, Any],
) -> None:
    """目标幕不得把来源幕精确交付重新演成又一次启动或确认。"""  # noqa: DOCSTRING_CJK

    segments = performance.get("segments")
    if not isinstance(segments, list) or len(segments) != 3:
        return
    target_performance = str(segments[2].get("performance") or "")
    target_anchors = set(_exact_goal_anchors(target_beat))
    repeated = [
        anchor
        for anchor in _exact_goal_anchors(source_beat)
        if anchor not in target_anchors and anchor in target_performance
    ]
    if repeated:
        raise NumericV2ActorOutputError("numeric_v2_actor_transition_repeats_source_goal")


def _opening_messages(
    engine: NumericV2Engine,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
    player_address_known: bool = True,
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    node = engine.nodes[str(engine.story["start_node_id"])]
    opening_contract = _acting_contract_for_actor(cast, node["story_beat"])
    player_questions_allowed = _player_suggestion_questions_allowed(opening_contract)
    suggestion_rule = (
        "所有 suggested_inputs 都只能是男主在当前开场之后可原样发送的下一步，并直接回应已显示的 performance。"
        "acting_context.acting_contract 中属于猫娘的提问、观察、自检、动作或台词必须由 performance 执行，"
        "不得复制或改写成男主候选。若猫娘需要地点、身份、原因或其他信息，performance 先由猫娘问出；"
        "候选让男主回答或主动披露，不能把同一信息需求倒置成男主提问。"
    )
    if not player_questions_allowed:
        suggestion_rule += (
            "当前是结构化开机且记忆空白状态；男主知道自己所在的现场，因此本次 suggested_inputs 禁止问句，"
            "只提供陈述、主动披露或男主自己的非接触动作。即使句末没有问号，也不得用祈使句让猫娘提供地点、"
            "身份、状态、判断或行动方案。"
            f"{_fresh_boot_suggestion_fact_rule()}"
        )
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
        "current_story_beat": _opening_beat_for_actor(cast, node["story_beat"]),
        "acting_context": _acting_context(
            engine,
            cast,
            node,
            engine.story["initial_state"]["metrics"],
            character_profile,
        ),
        "instruction": (
            "这是玩家输入前的公开开场。使用必要的环境或猫娘可见行动建立当下场景，再由猫娘主动说出第一句；"
            "不得假定玩家已经说话、做出选择或完成剧情摘要中的行动，不得使用‘你刚才说/做’或同义的隐形前史。"
            "若节点摘要包含玩家台词或主动行为，把它们视为后续可发展的剧情边界，不要在开场代替玩家执行。"
            "猫娘对白必须由本段旁白能够直接解释，并留下男主可以自然回应的话头；不要提前演完本节点。"
        ),
        # 开场没有普通回合的 owner 分流，必须单独声明猫娘职责与男主候选的方向，避免把女主问题倒置给玩家。
        "suggestion_instruction": {
            "mode": "opening_player_reply",
            "question_policy": (
                "allowed"
                if player_questions_allowed
                else "statements_and_actions_only"
            ),
            "rule": suggestion_rule,
        },
    }
    return _ensure_actor_messages_fit([
        SystemMessage(content=_system_prompt(
            catgirl_name=catgirl_name,
            player_address=player_address,
            player_address_known=player_address_known,
            phase="opening",
        )),
        HumanMessage(content=json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    ])


def _turn_messages(
    engine: NumericV2Engine,
    session: ScriptSessionV2,
    outcome: TurnOutcomeV2,
    player_input: str,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
    player_address_known: bool = True,
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    source = engine.nodes[str(outcome.ledger_event["from_node_id"])]
    target = engine.nodes[str(outcome.ledger_event["to_node_id"])]
    route_changed = source["id"] != target["id"]
    visible_node = target if route_changed else source
    visible_contract = _acting_contract_for_actor(cast, visible_node["story_beat"])
    player_questions_allowed = _player_suggestion_questions_allowed(visible_contract)
    system_prompt = _system_prompt(
        catgirl_name=catgirl_name,
        player_address=player_address,
        player_address_known=player_address_known,
        phase="transition" if route_changed else "turn",
    )
    current_player_input = str(player_input or "")
    human_prefix = "以下 JSON 是已确定性结算的本回合数据：\n"
    soft_pacing = _soft_pacing(
        source,
        session.node_turn_count + 1,
        route_changed=route_changed,
        scene_completion_ready=outcome.session.scene_completion_ready,
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
    player_address_state = {
        "known": player_address_known,
        "projection": player_address,
    }
    recent_context = _history(
        session,
        max_tokens=NUMERIC_V2_ACTOR_HISTORY_MAX_TOKENS,
    )
    if route_changed:
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
        target_opening = str(target_beat["opening_scene"])
        transition_contract = _transition_contract_for_actor(
            cast,
            outcome.transition_contract,
            target_opening=target_opening,
            target_goals=list(target_beat.get("pending_goals") or []),
        )
        suggestion_instruction = None
        if not player_questions_allowed:
            # 换场后的推荐属于目标节点；目标仍是开机空白态时，继续保留同一玩家职责边界。
            suggestion_instruction = {
                "question_policy": "statements_and_actions_only",
                "rule": (
                    "目标节点仍是结构化开机且记忆空白状态；suggested_inputs 禁止问句，只提供男主的陈述、"
                    "主动披露、明确决定或非接触动作。即使句末没有问号，也不得用祈使句让猫娘提供地点、身份、"
                    "状态、判断或行动方案。猫娘的信息需求必须由 target_opening.performance 提出。"
                    f"{_fresh_boot_suggestion_fact_rule()}"
                ),
            }
        data = _transition_prompt_data(
            story_context=story_context,
            player_address_state=player_address_state,
            current_chapter_title=_chapter_title_for_actor(cast, source),
            target_chapter_title=_chapter_title_for_actor(cast, target),
            shared_boundaries=shared_boundaries,
            source_boundaries=source_boundaries,
            target_boundaries=target_boundaries,
            source_completed_exact_anchors=_exact_goal_anchors(source["story_beat"]),
            target_story_beat=_transition_beat_for_actor(cast, target["story_beat"]),
            runtime_target_opening=target_opening,
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
            ),
            player_input=current_player_input,
            suggestion_instruction=suggestion_instruction,
        )
        data = _fit_turn_prompt_data(
            system_prompt=system_prompt,
            human_prefix=human_prefix,
            data=data,
        )
        return [
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prefix + json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
        ]

    # 普通回合保留节奏、推荐语和互动回收字段，换场不会再经过这条分支。
    data: dict[str, Any] = {
        "story_context": story_context,
        "player_address_state": player_address_state,
        "current_chapter_title": _chapter_title_for_actor(cast, source),
        "node_turn": session.node_turn_count + 1,
        "minimum_turns_before_route": int(source.get("min_turns") or 1),
        "route_changed": False,
        "soft_pacing": soft_pacing,
        "turn_instruction": "本回合留在当前节点，只推进当前互动所需的一小步。",
        "continuity_rule": "recent_context 是唯一已发生事实；节点目标不能覆盖或改写其中的时间、物品、行为与关系。",
        "current_story_beat": _beat_for_actor(
            cast,
            source["story_beat"],
            # 普通回合使用本轮 Evaluator 已结算的候选状态，让 Actor 立即跳过新完成目标。
            goal_evidence=outcome.session.scene_goal_evidence,
            # Runtime 锁存完成后会清理短期证据；显式传入完成事实，避免 Actor 把整组目标重新演一遍。
            scene_completion_ready=outcome.session.scene_completion_ready,
        ),
    }
    response_tail = {
        "recent_openings": _recent_openings(session),
        "recent_suggestions": _recent_suggestions(session),
        "acting_context": _acting_context(
            engine,
            cast,
            source,
            outcome.session.metrics,
            character_profile,
            relationship_metrics=session.metrics,
            target=None,
        ),
        "response_instruction": (
            "先回应 player_input；recent_context 是唯一已发生事实，只推进 current_story_beat，"
            "来源幕已结束，不得回去重新签约、交付、相识或复演核心事件。recent_suggestions 仅用于避重复，"
            "新推荐不得只是近期候选的同义改写。"
            "姓名、日期、编号、地点来历或完整故事只取 story_context、recent_context、current_story_beat；"
            "缺失就明确未知，不补具体身份、经历、约定或关键物品。"
            "player_input 是本轮已经发生的正式输入：男主明确说将执行某事时，回应这个决定，不得再问他是否要做；"
            "男主只报告‘没有变化’时，不得扩大成‘没有危险’或其他未给出的结论。"
            "玩家用第一人称描述的饥饿、感受、用途、对象与行动只属于男主，不得转移给猫娘或改写成另一种动机。"
            "玩家提出尚未确认的能力或部件名时，不得复述成猫娘确实拥有；只能说明无法确认它是否存在。"
            "微动作优先回应本轮变化；不要连续复用 recent_context 中相同的耳廓、瞳孔、指尖或尾巴动作组合。"
        ),
        "player_input": current_player_input,
    }
    if not session.performance_history:
        # opening 已经由前端展示；首个普通回合必须回应玩家，不能把开场当成本轮待生成任务再次交付。
        response_tail["response_instruction"] += (
            "这是公开开场后的首个普通回合；recent_context 中的 opening 已经发生，"
            "不得复述或近义重演其中的启动动作与对白，必须直接回应本轮 player_input。"
        )
    pending_goals = list(data["current_story_beat"].get("pending_goals") or [])
    if (
        outcome.session.scene_completion_ready
        and soft_pacing["phase"] in {"focus", "guided", "closure"}
    ):
        # 目标已完成但最短停留尚未结束时，最后一个普通回合只收束当前互动，不再制造新支线。
        response_tail["suggestion_instruction"] = {
            "mode": "close_current",
            "rule": (
                "performance 先回应 player_input，并自然收住当前互动。suggested_inputs 只提供男主可原样发送的确认、"
                "回应或结束当前话题的表达，不重演已完成目标，不开启新支线、任务、约定或关键物品；"
                "换幕只由 Runtime 决定。"
            ),
        }
    elif soft_pacing["phase"] == "closure" and pending_goals:
        # 到达建议回合后全部候选收束到剩余目标，仍只提供男主可自行选择的输入。
        response_tail["suggestion_instruction"] = {
            "mode": "mainline_closure",
            "goal_source": "current_story_beat.pending_goals",
            "rule": (
                "performance 先回应 player_input。全部 suggested_inputs 都必须是男主点击后可原样发送并直接推动 goal_source，"
                "不得延伸琐事或开启新话题。跳过 recent_context 已发生部分；owner=catgirl/environment 由 performance 推进，"
                "候选只让男主回应或披露，不得照抄提问或反问猫娘；owner=player/shared 只写候选、不代做。"
                "推荐始终可忽略，performance 不得催迫男主本回合作决定，也不得编造截止日期或外部催促。"
                "关键物品服从 factual_guard，不补未知答案；"
                "换幕只由 Runtime 决定。"
            ),
        }
    elif soft_pacing["phase"] == "guided" and pending_goals:
        # 达到 min_turns 后每回合至少留一个主线入口，玩家仍可忽略推荐并继续自由演绎。
        response_tail["suggestion_instruction"] = {
            "mode": "mainline_pull",
            "goal_source": "current_story_beat.pending_goals",
            "rule": (
                "performance 先回应 player_input。第一条 suggested_inputs 必须可原样发送并直接推动 goal_source；"
                "其余候选可以探索相邻方向，但不得开无关话题。跳过 recent_context 已发生部分；"
                "owner=catgirl/environment 由 performance 推进，候选只回应或披露，不得照抄提问；"
                "owner=player/shared 只写候选、不代做。关键物品服从 factual_guard，不补未知答案；换幕只由 Runtime 决定。"
            ),
        }
    elif soft_pacing["phase"] == "closure":
        # 兼容没有结构化待办的旧包：强收束阶段不再制造新的支线承诺。
        response_tail["suggestion_instruction"] = {
            "mode": "close_current",
            "rule": (
                "performance 先回应 player_input，并自然收住当前互动。全部 suggested_inputs 只用于确认、回应或结束当前话题，"
                "不得开启新的支线、任务、约定或关键物品；换幕只由 Runtime 决定。"
            ),
        }
    else:
        # 到达 min_turns 前不把推荐输入绑死到主线，保留玩家探索空间。
        response_tail["suggestion_instruction"] = {
            "mode": "diverge",
            "rule": (
                "提供 2 到 4 条不同方向的台词或即时动作，可探索、试探、假设、披露男主信息或推进目标。"
                "候选发送前均未发生，且不得违反事实、关系上限或边界；关键物品服从 factual_guard，"
                "不补未知属性、功能或结果。"
                "不要为了提前换幕而强迫第一条命中 pending_goals。"
            ),
        }
    if not player_questions_allowed:
        # 认知状态属于当前节点而不是 opening 调用；留在同一开机幕时，普通回合也必须持续约束推荐角色。
        response_tail["suggestion_instruction"]["question_policy"] = "statements_and_actions_only"
        response_tail["suggestion_instruction"]["rule"] += (
            " 当前可见节点仍是结构化开机且记忆空白状态；suggested_inputs 禁止问句，只提供男主的陈述、"
            "主动披露、明确决定或非接触动作。即使句末没有问号，也不得用祈使句让猫娘提供地点、身份、状态、"
            "判断或行动方案。不得把男主已知的地点、身份、风险判断或行动决定反问给猫娘。"
            f"{_fresh_boot_suggestion_fact_rule()}"
        )
    # 普通氛围允许 Actor 即兴，只有身份、经历与推动目标/路线的关键物品维持严格事实边界；
    # suggested_inputs 是未来候选，只禁止它们把候选内容伪装成“已经发生”的记录。
    response_tail["factual_guard"] = (
        "最后核对：身份、经历、关系和已发生事件只取 story_context、recent_context、current_story_beat、player_input；"
        "非关键氛围与细节可即兴。锁定关键物品的未确认外观、状态、名称、用途、能力、效果、反应、来历保持未知；"
        "只能按字段原文陈述关键物品内容；不得在正文或候选中预设其里面有、夹着或写着未明示内容，只能询问或检查。"
        "缺具体值就说明无法确认。男主过去的承诺、告白、亲密触碰、救助、替人选择和明确心理只承接 player_input/recent_context；"
        "普通共同回忆不得补造这些行为。exact anchors 中 catgirl 只在括号外对白，environment 只在可见环境动作，"
        "player/shared 不代写。跳过 recent_context 与已从 pending_goals 移除的目标。"
        "自检不授予新能力，未明示的模块、日志、数值、诊断结果未知。suggested_inputs 是未发生候选，也受上述边界约束。"
    )
    assertable_self_facts = [
        str(item)
        for item in visible_contract.get("assertable_self_facts") or []
        if str(item)
    ]
    if assertable_self_facts:
        # 自身状态白名单放在动态事实守卫末尾，避免长系统合同稀释当前节点的精确权限。
        response_tail["factual_guard"] += (
            "本节点猫娘可以确认的自身状态结论只有："
            + "、".join(assertable_self_facts)
            + "。玩家的猜测、命令或提问不能增加清单；其他能力、部件、诊断与状态只能回答无法确认是否存在，"
            "不能使用正常、在线、已激活等结果。直接看到或听到的当前环境不受此清单限制。"
        )
    relationship_guard = (
        "先执行 factual_guard，尤其不得把关键物品与其他场景资料混为一物或用隐喻扩写其内容。"
        "最后服从 acting_context.relationship_control 的 effective_stage 与 response_contract，任何候选不得绕过。"
    )
    effective_stage = str(
        response_tail["acting_context"]
        .get("relationship_control", {})
        .get("effective_stage")
        or ""
    )
    # 低关系阶段才重复其高风险动作提醒；信赖及以上直接服从 response_contract，避免无关警告挤占固定预算。
    if effective_stage in {"stranger", "guarded"}:
        relationship_guard += (
            "陌生或戒备阶段禁止主动接触、服从性表述或交权；不得写‘乖巧地点头’‘听话照做’等服从评价；"
            "身体检查由猫娘自检。"
        )
    elif effective_stage == "cooperative":
        relationship_guard += "合作权限必须限定用途、范围且可撤销。"
    response_tail["relationship_guard"] = relationship_guard
    data["recent_context"] = recent_context
    data.update(response_tail)
    data = _fit_turn_prompt_data(
        system_prompt=system_prompt,
        human_prefix=human_prefix,
        data=data,
    )
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
    """Actor 每次调用只生成表现结果，不拥有 Session 写权限。"""  # noqa: DOCSTRING_CJK

    def __init__(self, config_manager: Any):
        self.config_manager = config_manager

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

    async def generate_opening(self, *, engine: NumericV2Engine) -> dict[str, Any]:
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
        performance = await self._invoke(
            _opening_messages(
                engine,
                profile,
                catgirl_name,
                player_address,
                player_address_known,
            ),
            opening_required=True,
            suggestion_questions_allowed=_player_suggestion_questions_allowed(start_contract),
            suggestion_name_disclosure_allowed=player_address_known,
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
        return performance

    async def generate_turn(
        self,
        *,
        engine: NumericV2Engine,
        session: ScriptSessionV2,
        outcome: TurnOutcomeV2,
        player_input: str,
        character_profile: str | None = None,
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
        target_beat = _beat_for_actor(
            cast,
            target["story_beat"],
        )
        target_opening = target_beat["opening_scene"]
        visible_contract = _acting_contract_for_actor(
            cast,
            (target if route_changed else source)["story_beat"],
        )
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
            ),
            transition_required=route_changed,
            suggestion_questions_allowed=_player_suggestion_questions_allowed(visible_contract),
            suggestion_name_disclosure_allowed=player_address_known,
            target_node_id=str(outcome.ledger_event["to_node_id"]),
            target_opening=target_opening,
            target_goals=list(target_beat.get("pending_goals") or []),
        )
        if route_changed:
            segments = performance.get("segments")
            source_output = segments[0] if isinstance(segments, list) and len(segments) == 3 else {}
            target_output = segments[2] if isinstance(segments, list) and len(segments) == 3 else {}
            _assert_transition_bridge_player_ownership(
                performance,
                player_address=configured_address,
            )
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
            _assert_transition_target_excludes_source_exact_anchors(
                performance,
                source_beat=source["story_beat"],
                target_beat=target["story_beat"],
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
        performance["suggested_inputs"] = _deduplicate_recent_suggestions(
            list(performance.get("suggested_inputs") or []),
            _recent_suggestions(session),
        )
        if route_changed:
            performance = engine.finalize_transition_performance(
                outcome,
                performance,
                target_opening=target_opening,
            )
        # 第一回合没有普通历史时，opening 就是玩家上一条看到的演绎；重复保护必须覆盖它。
        if session.performance_history:
            previous_visible_performance = session.performance_history[-1]
        else:
            previous_visible_performance = session.opening_performance
            if isinstance(previous_visible_performance.get("performance"), str):
                # 开场场景旁白不会在普通回合复现；比较时只取猫娘开场正文，避免长旁白稀释重复率。
                previous_visible_performance = {
                    "performance": previous_visible_performance["performance"],
                }
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
            logger.warning(
                "Numeric v2 Actor failed: reason=numeric_v2_actor_repeated_output session_id=%s revision=%s",
                session.session_id,
                session.revision,
            )
            raise NumericV2ActorOutputError("numeric_v2_actor_repeated_output")
        return performance

    async def _invoke(
        self,
        messages: list[Any],
        *,
        opening_required: bool = False,
        transition_required: bool = False,
        suggestion_questions_allowed: bool = True,
        suggestion_name_disclosure_allowed: bool = True,
        target_node_id: str = "",
        target_opening: str = "",
        target_goals: list[str] | None = None,
    ) -> dict[str, Any]:
        set_call_type("theater_numeric_v2_actor")
        request_messages = _ensure_actor_messages_fit(messages)
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
                    max_completion_tokens=NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS,
                )
                client_finished_at = time.monotonic()
                async with client:
                    # request_messages 已由 _ensure_actor_messages_fit 按模型输入预算裁剪。
                    response = await client.ainvoke(request_messages)  # noqa: LLM_INPUT_BUDGET
                    request_finished_at = time.monotonic()
                    parsed = _parse_output(
                        getattr(response, "content", None),
                        opening_required=opening_required,
                        transition_required=transition_required,
                        suggestion_questions_allowed=suggestion_questions_allowed,
                        suggestion_name_disclosure_allowed=suggestion_name_disclosure_allowed,
                        target_node_id=target_node_id,
                        target_opening=target_opening,
                        target_goals=target_goals,
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
    "NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS",
    "NUMERIC_V2_ACTOR_TIMEOUT_SECONDS",
    "NumericV2Actor",
    "NumericV2ActorError",
    "NumericV2ActorOutputError",
    "NumericV2ActorUnavailableError",
]
