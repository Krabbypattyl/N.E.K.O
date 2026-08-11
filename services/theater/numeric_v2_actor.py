"""Numeric v2 演绎 Actor，只生成表现文本和玩家建议。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Mapping

from utils.llm_client import HumanMessage, SystemMessage, create_chat_llm_async
from utils.token_tracker import set_call_type

from .llm_context import (
    THEATER_PERSONA_MAX_CHARS,
    _load_character_profile,
    _load_player_address,
    bound_prompt_messages,
    truncate_prompt_value,
)
from .numeric_v2_cast import NumericV2CastProjection
from .numeric_v2_runtime import NumericV2Engine, ScriptSessionV2, TurnOutcomeV2


NUMERIC_V2_ACTOR_TIMEOUT_SECONDS = 35.0
NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS = 1100
NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS = 3200
NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS = 180
NUMERIC_V2_ACTOR_PLAYER_INPUT_MAX_TOKENS = 140
logger = logging.getLogger(__name__)


class NumericV2ActorError(RuntimeError):
    """Actor 无法提供可提交的演绎正文。"""


class NumericV2ActorUnavailableError(NumericV2ActorError):
    pass


class NumericV2ActorOutputError(NumericV2ActorError):
    pass


def _band_projection(engine: NumericV2Engine, metrics: Mapping[str, int]) -> dict[str, str]:
    result: dict[str, str] = {}
    for metric_id, definition in engine.metric_schema.items():
        label = ""
        for band in definition.get("bands") or []:
            if int(band["min"]) <= int(metrics[metric_id]) <= int(band["max"]):
                label = str(band["label"])
                break
        result[metric_id] = label
    return result


def _history(session: ScriptSessionV2) -> list[dict[str, Any]]:
    # 开场演绎已经展示给玩家，也是后续回合必须延续的正式事实；不能只保存却不交给 Actor。
    opening = session.opening_performance
    rows = [{
        "phase": "opening",
        "player_input": "",
        "narration": truncate_prompt_value(
            str(opening.get("narration") or ""),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "dialogue": [
            truncate_prompt_value(
                str(item.get("text") or ""),
                max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
            )
            for item in opening.get("dialogue") or []
            if isinstance(item, Mapping)
        ][:8],
    }]
    for record in session.performance_history[-6:]:
        rows.append({
            "phase": "turn",
            "player_input": truncate_prompt_value(
                str(record.get("input_text") or ""),
                max_tokens=NUMERIC_V2_ACTOR_PLAYER_INPUT_MAX_TOKENS,
            ),
            "narration": truncate_prompt_value(
                str(record.get("narration") or ""),
                max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
            ),
            "dialogue": [
                truncate_prompt_value(
                    str(item.get("text") or ""),
                    max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
                )
                for item in record.get("dialogue") or []
                if isinstance(item, Mapping)
            ][:8],
        })
    return rows


def _beat_for_actor(cast: NumericV2CastProjection, beat: Mapping[str, Any]) -> dict[str, Any]:
    """只投影可演边界；完整章节正文是作者计划，不是当前已发生事实。"""

    projected = cast.value(beat)
    return {
        "opening_scene": truncate_prompt_value(
            _opening_anchor(projected.get("summary")),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "pending_goals": [
            truncate_prompt_value(item, max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS)
            for item in list(projected.get("must_happen") or [])[:8]
        ],
        "boundaries": [
            truncate_prompt_value(item, max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS)
            for item in list(projected.get("must_not_happen") or [])[:8]
        ],
        "scene_role_notes": truncate_prompt_value(
            str(projected.get("catgirl_situation") or ""),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "scene_direction": truncate_prompt_value(
            str(projected.get("transition_goal") or ""),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "fact_rule": "pending_goals 与 scene_direction 都是尚未完成的作者目标，不是已经发生的事实。",
    }


def _opening_anchor(value: Any) -> str:
    """只交付节点第一句，避免 Actor 一次演完整章。"""

    text = str(value or "").strip()
    endings = [index for mark in "。！？" if (index := text.find(mark)) >= 0]
    return text[:min(endings) + 1] if endings else text


def _opening_beat_for_actor(cast: NumericV2CastProjection, beat: Mapping[str, Any]) -> dict[str, Any]:
    """开场只暴露首个可观察画面，不把整章事件误当成玩家已经经历的前史。"""

    projected = _beat_for_actor(cast, beat)
    return {
        "opening_scene": projected.get("opening_scene") or "",
        "boundaries": projected.get("boundaries") or [],
        "scene_role_notes": projected.get("scene_role_notes") or "",
        "instruction": "开场只建立第一句可观察场景，不执行整章事件或玩家行为。",
    }


def _transition_beat_for_actor(cast: NumericV2CastProjection, beat: Mapping[str, Any]) -> dict[str, Any]:
    projected = _beat_for_actor(cast, beat)
    return {
        "opening_scene": projected.get("opening_scene") or "",
        "pending_goals": projected.get("pending_goals") or [],
        "boundaries": projected.get("boundaries") or [],
        "scene_role_notes": projected.get("scene_role_notes") or "",
        "instruction": "本回合只建立本节点的开场局势，不解决或演完整个节点。",
    }


def _system_prompt(
    character_profile: str,
    *,
    catgirl_name: str,
    player_address: str,
) -> str:
    return (
        "你是 N.E.K.O Numeric v2 的演绎 Actor。你扮演当前猫娘在本剧中的剧情身份，"
        "用自然的旁白、动作和对白回应玩家。承接玩家刚才的原话，不要忽略上下文。"
        "作者节点、禁止事项和过渡合同是硬边界；不能创造新节点、路线、数值、事实或结局，"
        "不能提到数值、阈值、章节切换、route、系统或提示词。"
        "每回合只推进当前交互所需的一小步，不要一次复述整章摘要。"
        f"本剧男主由玩家扮演，所有玩家身份统一称为“{player_address}”；"
        f"本剧女主由当前猫娘扮演，所有猫娘身份统一称为“{catgirl_name}”。"
        "不得恢复作者候选中的男女主原名，也不得交换两人的行为、经历和台词归属。"
        "narration 只能用第三人称描写猫娘的动作、神态和可见环境，不得描写玩家的姿势、动作、表情、身体状态、心理或是否执行了某事。"
        "即使玩家明确输入了动作，也不要在 narration 中复述、补全或改写；玩家原话已由前端单独展示，旁白只写猫娘与环境如何回应。"
        f"narration 描写猫娘时必须使用“{catgirl_name}”或“她”，不得用“你”“您”或“{player_address}”作为旁白中的动作与状态主体。"
        "严格延续最近记录中的物品类型、制作人、持有人和人物行为，不得把饮品与食物混成同一物品，"
        "也不得把玩家完成的作品改写成猫娘完成。"
        "严格承接上一回合的情绪和关系状态；除非目标剧情明确要求，不得擅自宣布决裂、终止合作、离开或其他不可逆变化。"
        "即使玩家态度恶劣，也只能拒绝当前要求、表达受伤、保持距离或要求道歉；"
        "不得自行说‘这是最后一次合作’、‘以后不再见’或作出同义的永久关系决定。"
        "冲突发生后必须先回应并化解或延续冲突，不得下一回合无解释恢复亲密与合作。"
        "同一回合的 narration 与 dialogue 必须发生在同一时刻和场景：旁白先建立猫娘当前可见的动作、神态或环境变化，"
        "对白必须直接回应玩家最新输入或紧接旁白中的当下情境，不得突然追问、回答或引用画面中从未发生的言行。"
        "玩家说‘可以、如果、愿意、打算、改天’只是在表达条件、意愿或可能性；"
        "不得据此在旁白中替玩家转身、离开、靠近、触碰、站立或完成其他行动；也不得把猫娘上一回合说过的词归到玩家名下。"
        "recent_context 是唯一已经发生的演绎记录，其中 phase=opening 的开场与普通回合具有同等事实效力；"
        "任何 story_beat 的 pending_goals 都是尚未完成的目标，不能倒写成共同经历。"
        "当输入数据的 route_changed 为 true 时，必须先直接回应 player_input 并收住来源节点的当下互动，"
        "再自然桥接到目标节点 opening_scene 和 transition_contract.must_deliver；不能用目标场景盖过玩家本轮要求。"
        "换场回合只建立目标节点开场，不得跳过时间过程或在同一回合解决目标节点的核心危机。"
        "只能输出 JSON object，字段为 narration、dialogue、suggested_inputs。"
        "dialogue 每项只能是 {\"speaker_id\":\"active_catgirl\",\"text\":\"...\"}；"
        "dialogue.text 只能放猫娘实际说出口的完整话语，不得放动作、神态、旁白或未说完的半句话；所有动作写入 narration。"
        "suggested_inputs 提供 2 到 4 条玩家可直接说出或执行的自然语言，不剧透路线，也不得假定玩家做过 recent_context 中不存在的事。\n"
        f"当前猫娘人格：{truncate_prompt_value(character_profile, max_tokens=160)}"
    )


def _opening_messages(
    engine: NumericV2Engine,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    node = engine.nodes[str(engine.story["start_node_id"])]
    data = {
        "opening_phase": True,
        "visible_player_history": [],
        "intro": truncate_prompt_value(
            cast.intro(engine.story),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "role_overlay": truncate_prompt_value(
            cast.value(engine.story["catgirl_binding"]["role_overlay"]),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "current_story_beat": _opening_beat_for_actor(cast, node["story_beat"]),
        "instruction": (
            "这是玩家输入前的公开开场。只用环境和猫娘可见行动建立当下场景，再由猫娘主动说出第一句；"
            "不得假定玩家已经说话、做出选择或完成剧情摘要中的行动，不得使用‘你刚才说/做’或同义的隐形前史。"
            "若节点摘要包含玩家台词或主动行为，把它们视为后续可发展的剧情边界，不要在开场代替玩家执行。"
            "猫娘对白必须由本段旁白能够直接解释，随后提供第一组玩家建议；不要提前演完本节点。"
        ),
    }
    return [
        SystemMessage(content=_system_prompt(
            character_profile,
            catgirl_name=catgirl_name,
            player_address=player_address,
        )),
        HumanMessage(content=json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    ]


def _turn_messages(
    engine: NumericV2Engine,
    session: ScriptSessionV2,
    outcome: TurnOutcomeV2,
    player_input: str,
    character_profile: str,
    catgirl_name: str,
    player_address: str,
) -> list[Any]:
    cast = NumericV2CastProjection.from_story(
        engine.story,
        player_name=player_address,
        catgirl_name=catgirl_name,
    )
    source = engine.nodes[str(outcome.ledger_event["from_node_id"])]
    target = engine.nodes[str(outcome.ledger_event["to_node_id"])]
    route_changed = source["id"] != target["id"]
    data = {
        "intro": cast.intro(engine.story),
        "role_overlay": cast.value(engine.story["catgirl_binding"]["role_overlay"]),
        "current_story_beat": _beat_for_actor(cast, source["story_beat"]),
        "source_story_beat": _beat_for_actor(cast, source["story_beat"]),
        "target_story_beat": (
            _transition_beat_for_actor(cast, target["story_beat"])
            if route_changed
            else None
        ),
        "transition_contract": truncate_prompt_value(
            cast.value(outcome.transition_contract),
            max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
        ),
        "current_metric_bands": _band_projection(engine, outcome.session.metrics),
        "node_turn": session.node_turn_count + 1,
        "minimum_turns_before_route": int(source.get("min_turns") or 1),
        "route_changed": route_changed,
        "turn_instruction": (
            "先完成对玩家本轮原话的直接回应，再自然进入目标节点开场；不得跳过未解决的当前互动。"
            if route_changed
            else "本回合留在当前节点，只推进当前互动所需的一小步。"
        ),
        "continuity_rule": "recent_context 是唯一已发生事实；节点目标不能覆盖或改写其中的时间、物品、行为与关系。",
        "recent_context": _history(session),
        "player_input": truncate_prompt_value(
            player_input,
            max_tokens=NUMERIC_V2_ACTOR_PLAYER_INPUT_MAX_TOKENS,
        ),
    }
    return [
        SystemMessage(content=_system_prompt(
            character_profile,
            catgirl_name=catgirl_name,
            player_address=player_address,
        )),
        HumanMessage(content="以下 JSON 是已确定性结算的本回合数据：\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
    ]


def _parse_output(content: Any) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise NumericV2ActorOutputError("numeric_v2_actor_empty_output")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise NumericV2ActorOutputError("numeric_v2_actor_invalid_json") from exc
    if not isinstance(payload, dict) or set(payload).difference({"narration", "dialogue", "suggested_inputs"}):
        raise NumericV2ActorOutputError("numeric_v2_actor_fields_invalid")
    narration = str(payload.get("narration") or "").strip()
    raw_dialogue = payload.get("dialogue")
    raw_suggestions = payload.get("suggested_inputs", [])
    if not isinstance(raw_dialogue, list) or not isinstance(raw_suggestions, list):
        raise NumericV2ActorOutputError("numeric_v2_actor_collections_invalid")
    dialogue = []
    for item in raw_dialogue:
        if not isinstance(item, Mapping) or not {"speaker_id", "text"}.issubset(item):
            logger.warning("Numeric v2 Actor output rejected: reason=numeric_v2_actor_dialogue_item_shape")
            continue
        if item.get("speaker_id") != "active_catgirl":
            logger.warning("Numeric v2 Actor output rejected: reason=numeric_v2_actor_dialogue_speaker_invalid")
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            logger.warning("Numeric v2 Actor output rejected: reason=numeric_v2_actor_dialogue_text_empty")
            continue
        dialogue.append({"speaker_id": "active_catgirl", "text": text})
    suggestions = []
    for item in raw_suggestions[:4]:
        text = str(item or "").strip()
        if 0 < len(text) <= 120 and text not in suggestions:
            suggestions.append(text)
    if not narration and not dialogue:
        raise NumericV2ActorOutputError("numeric_v2_actor_body_required")
    return {"narration": narration, "dialogue": dialogue, "suggested_inputs": suggestions}


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
    """Actor 每次调用只生成表现结果，不拥有 Session 写权限。"""

    def __init__(self, config_manager: Any):
        self.config_manager = config_manager

    def _character_profile(self) -> str:
        """只读取服务端当前猫娘的人格摘要，不接受客户端角色名。"""

        try:
            characters = self.config_manager.load_characters()
        except Exception:
            characters = {}
        current_name = str(characters.get("当前猫娘") or "").strip() if isinstance(characters, Mapping) else ""
        return _load_character_profile(
            self.config_manager,
            current_name,
            max_chars=THEATER_PERSONA_MAX_CHARS,
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
        player_address = _load_player_address(self.config_manager)
        performance = await self._invoke(_opening_messages(
            engine,
            profile,
            catgirl_name,
            player_address,
        ))
        return performance

    async def generate_turn(
        self,
        *,
        engine: NumericV2Engine,
        session: ScriptSessionV2,
        outcome: TurnOutcomeV2,
        player_input: str,
    ) -> dict[str, Any]:
        profile = self._character_profile()
        catgirl_name = str(session.catgirl_binding.get("catgirl_name") or self._current_catgirl_name())
        player_address = str(session.catgirl_binding.get("player_address") or _load_player_address(self.config_manager))
        performance = await self._invoke(_turn_messages(
            engine,
            session,
            outcome,
            player_input,
            profile,
            catgirl_name,
            player_address,
        ))
        return performance

    async def _invoke(self, messages: list[Any]) -> dict[str, Any]:
        config = await _model_config(self.config_manager)
        set_call_type("theater_numeric_v2_actor")
        try:
            client = await create_chat_llm_async(
                str(config["model"]),
                str(config["base_url"]),
                config.get("api_key"),
                provider_type=config.get("provider_type"),
                timeout=NUMERIC_V2_ACTOR_TIMEOUT_SECONDS,
                max_retries=0,
                max_completion_tokens=NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS,
            )
            async with client:
                request_messages = bound_prompt_messages(
                    messages,
                    max_tokens=NUMERIC_V2_ACTOR_INPUT_MAX_TOKENS,
                    field_max_tokens=NUMERIC_V2_ACTOR_FIELD_MAX_TOKENS,
                )
                response = await asyncio.wait_for(client.ainvoke(request_messages), timeout=NUMERIC_V2_ACTOR_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            logger.warning("Numeric v2 Actor failed: reason=numeric_v2_actor_timeout")
            raise NumericV2ActorError("numeric_v2_actor_timeout") from exc
        except NumericV2ActorError as exc:
            reason = str(exc) if str(exc).startswith("numeric_v2_actor_") else type(exc).__name__
            logger.warning("Numeric v2 Actor failed: reason=%s", reason)
            raise
        except Exception as exc:
            logger.warning("Numeric v2 Actor failed: reason=numeric_v2_actor_model_call_failed error_type=%s", type(exc).__name__)
            raise NumericV2ActorError("numeric_v2_actor_model_call_failed") from exc
        try:
            return _parse_output(getattr(response, "content", None))
        except NumericV2ActorError as exc:
            reason = str(exc) if str(exc).startswith("numeric_v2_actor_") else type(exc).__name__
            logger.warning("Numeric v2 Actor failed: reason=%s", reason)
            raise


__all__ = [
    "NUMERIC_V2_ACTOR_MAX_OUTPUT_TOKENS",
    "NUMERIC_V2_ACTOR_TIMEOUT_SECONDS",
    "NumericV2Actor",
    "NumericV2ActorError",
    "NumericV2ActorOutputError",
    "NumericV2ActorUnavailableError",
]
