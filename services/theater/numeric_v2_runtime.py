"""Numeric v2 的确定性状态引擎与 Runtime 入口。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
import re
from pathlib import Path
from typing import Any, Mapping

from utils.tokenize import truncate_to_tokens

from .numeric_v2 import (
    CompiledNumericV2Package,
    NumericV2Compiler,
    numeric_v2_story_goal_contracts,
)
from .numeric_v2_performance import (
    valid_mixed_performance,
    valid_ordered_content,
    valid_scene_narration,
)
from .numeric_v2_store import (
    NumericV2SessionStore,
    NumericV2StoredSession,
)


SESSION_SCHEMA = "neko.script.session.numeric.v2"
LEDGER_EVENT_SCHEMA = "neko.script.ledger_event.numeric.v2"
PERFORMANCE_RECORD_SCHEMA = "neko.script.performance_record.numeric.v2"
NUMERIC_V2_PLAYER_INPUT_MAX_TOKENS = 140
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMPARATORS = {
    "==": lambda left, right: left == right,
    "!=": lambda left, right: left != right,
    ">": lambda left, right: left > right,
    "<": lambda left, right: left < right,
    ">=": lambda left, right: left >= right,
    "<=": lambda left, right: left <= right,
}


class NumericV2RuntimeError(ValueError):
    """Numeric v2 回合无法在当前确定性状态上结算。"""  # noqa: DOCSTRING_CJK


class NumericV2RevisionConflictError(NumericV2RuntimeError):
    """客户端基于过期 revision 提交。"""  # noqa: DOCSTRING_CJK


class NumericV2DuplicateTurnError(NumericV2RuntimeError):
    """同一个 client_turn_id 已经成功提交。"""  # noqa: DOCSTRING_CJK


def _stable_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise NumericV2RuntimeError(f"{field}_invalid")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NumericV2RuntimeError(f"{field}_invalid")
    return value


def _player_address_disclosed(message: str, configured_address: str) -> bool:
    """只接受包含完整昵称的明确自我介绍或称呼请求。"""  # noqa: DOCSTRING_CJK

    text = str(message or "").strip()
    address = str(configured_address or "").strip()
    if not text or not address or address in {"你", "男主"}:
        return False
    escaped = re.escape(address)
    left = r"(?:^|[\s,，。.!！;；:：])"
    right = r"(?=$|[\s,，。.!！;；:：])"
    quoted_address = rf"[\"'“‘「『]?{escaped}[\"'”’」』]?"
    patterns = (
        # 中文：限定为第一人称身份陈述或明确的称呼指令，排除“你认识小明吗”。
        rf"{left}(?:我(?:的名字)?(?:是|叫)|请?(?:叫|称呼)我(?:为)?|你可以叫我)\s*{quoted_address}(?:吧|就好|即可)?{right}",
        rf"{left}{quoted_address}\s*(?:就是我|是我){right}",
        # 其他已支持界面的常见自我介绍形式；昵称本身始终按完整精确字符串匹配。
        rf"{left}(?:i\s+am|i['’]m|my\s+name\s+is|call\s+me|you\s+can\s+call\s+me)\s+{quoted_address}{right}",
        rf"{left}(?:me\s+llamo|ll[aá]mame|me\s+chamo|pode\s+me\s+chamar\s+de|меня\s+зовут)\s+{quoted_address}{right}",
        rf"{left}(?:私は|僕は|俺は|名前は)\s*{quoted_address}\s*(?:です|だ){right}",
        rf"{left}{quoted_address}\s*と呼んで{right}",
        rf"{left}(?:저는|나는|제\s*이름은|내\s*이름은)\s*{quoted_address}\s*(?:입니다|예요|이에요){right}",
        rf"{left}{quoted_address}\s*(?:라고|이라고)\s*불러{right}",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _player_address_known_after_turn(
    session: "ScriptSessionV2",
    message: str,
) -> bool:
    """仅在玩家明确披露完整配置昵称后推进称呼知情状态。"""  # noqa: DOCSTRING_CJK

    if session.player_address_known:
        return True
    configured_address = str(session.catgirl_binding.get("player_address") or "").strip()
    return _player_address_disclosed(message, configured_address)


def _scene_completion_ready(value: Mapping[str, Any]) -> bool:
    raw = value.get("scene_completion_ready")
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise NumericV2RuntimeError("scene_completion_ready_invalid")
    return raw


def _scene_goal_evidence(value: Any) -> dict[str, tuple[int, ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise NumericV2RuntimeError("scene_goal_evidence_invalid")
    result: dict[str, tuple[int, ...]] = {}
    for goal_id, revisions in value.items():
        if not isinstance(revisions, (list, tuple)):
            raise NumericV2RuntimeError("scene_goal_evidence_invalid")
        result[str(goal_id)] = tuple(
            _integer(revision, "scene_goal_evidence_revision")
            for revision in revisions
        )
    return result


@dataclass(frozen=True, slots=True)
class MetricChangeV2:
    """判定模型提出、确定性引擎复验后的单项数值变化。"""  # noqa: DOCSTRING_CJK

    metric_id: str
    delta: int
    criterion: str
    evidence: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        metric_schema: Mapping[str, Any],
    ) -> "MetricChangeV2":
        if set(value) != {"metric_id", "delta", "criterion", "evidence"}:
            raise NumericV2RuntimeError("metric_change_fields_invalid")
        metric_id = _stable_id(value.get("metric_id"), "metric_id")
        if metric_id not in metric_schema:
            raise NumericV2RuntimeError("metric_change_unknown_metric")
        delta = _integer(value.get("delta"), "metric_delta")
        if delta == 0:
            raise NumericV2RuntimeError("metric_delta_zero")
        definition = metric_schema[metric_id]
        direction = "increase" if delta > 0 else "decrease"
        limit = int(definition["per_turn_limit"][direction])
        if abs(delta) > limit:
            raise NumericV2RuntimeError("metric_delta_limit_exceeded")
        criterion = str(value.get("criterion") or "").strip()
        evidence = str(value.get("evidence") or "").strip()
        if not criterion or not evidence:
            raise NumericV2RuntimeError("metric_change_reason_required")
        # 判定器只能命中作者已声明的依据，不能借自由文本扩展数值规则。
        allowed_criteria = definition[f"{direction}_criteria"]
        if criterion not in allowed_criteria:
            raise NumericV2RuntimeError("metric_change_criterion_invalid")
        return cls(metric_id, delta, criterion, evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "delta": self.delta,
            "criterion": self.criterion,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class TurnRequestV2:
    client_turn_id: str
    base_revision: int
    message: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TurnRequestV2":
        request = cls(
            client_turn_id=_stable_id(value.get("client_turn_id"), "client_turn_id"),
            base_revision=_integer(value.get("base_revision"), "base_revision"),
            message=str(value.get("message") or "").strip(),
        )
        if request.base_revision < 0 or not request.message:
            raise NumericV2RuntimeError("numeric_turn_request_invalid")
        if truncate_to_tokens(request.message, NUMERIC_V2_PLAYER_INPUT_MAX_TOKENS) != request.message:
            raise NumericV2RuntimeError("numeric_turn_input_too_long")
        return request


@dataclass(frozen=True, slots=True)
class ScriptSessionV2:
    session_id: str
    story_package_id: str
    story_package_revision: str
    story_package_hash: str
    catgirl_binding: dict[str, str]
    current_node_id: str
    metrics: dict[str, int]
    node_turn_count: int
    revision: int
    status: str
    processed_client_turn_ids: tuple[str, ...]
    opening_performance: dict[str, Any]
    performance_history: tuple[dict[str, Any], ...]
    player_address_known: bool = False
    ended_reason: str | None = None
    # 本幕目标一旦被 Evaluator 判定完成，就保持到满足 min_turns 或离开节点。
    # 旧 Session 缺少这两个字段时使用空状态，恢复后从下一回合开始采用新合同。
    scene_completion_ready: bool = False
    scene_goal_evidence: dict[str, tuple[int, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA,
            "session_id": self.session_id,
            "story_package_id": self.story_package_id,
            "story_package_revision": self.story_package_revision,
            "story_package_hash": self.story_package_hash,
            "catgirl_binding": deepcopy(self.catgirl_binding),
            "current_node_id": self.current_node_id,
            "metrics": dict(self.metrics),
            "node_turn_count": self.node_turn_count,
            "revision": self.revision,
            "status": self.status,
            "processed_client_turn_ids": list(self.processed_client_turn_ids),
            "opening_performance": deepcopy(self.opening_performance),
            "performance_history": deepcopy(list(self.performance_history)),
            "player_address_known": self.player_address_known,
            "ended_reason": self.ended_reason,
            "scene_completion_ready": self.scene_completion_ready,
            "scene_goal_evidence": {
                goal_id: list(revisions)
                for goal_id, revisions in self.scene_goal_evidence.items()
            },
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScriptSessionV2":
        if value.get("schema") != SESSION_SCHEMA:
            raise NumericV2RuntimeError("numeric_session_schema_invalid")
        raw_player_address_known = value.get("player_address_known", False)
        if not isinstance(raw_player_address_known, bool):
            raise NumericV2RuntimeError("session_player_address_known_invalid")
        return cls(
            session_id=_stable_id(value.get("session_id"), "session_id"),
            story_package_id=_stable_id(value.get("story_package_id"), "story_package_id"),
            story_package_revision=str(value.get("story_package_revision") or ""),
            story_package_hash=str(value.get("story_package_hash") or ""),
            catgirl_binding=deepcopy(dict(value.get("catgirl_binding") or {})),
            current_node_id=_stable_id(value.get("current_node_id"), "current_node_id"),
            metrics={str(key): _integer(item, "session_metric") for key, item in dict(value.get("metrics") or {}).items()},
            node_turn_count=_integer(value.get("node_turn_count"), "node_turn_count"),
            revision=_integer(value.get("revision"), "revision"),
            status=str(value.get("status") or ""),
            processed_client_turn_ids=tuple(str(item) for item in value.get("processed_client_turn_ids") or []),
            opening_performance=deepcopy(dict(value.get("opening_performance") or {})),
            performance_history=tuple(deepcopy(list(value.get("performance_history") or []))),
            player_address_known=raw_player_address_known,
            ended_reason=(str(value.get("ended_reason")) if value.get("ended_reason") is not None else None),
            scene_completion_ready=_scene_completion_ready(value),
            scene_goal_evidence=_scene_goal_evidence(value.get("scene_goal_evidence")),
        )


@dataclass(frozen=True, slots=True)
class TurnOutcomeV2:
    session: ScriptSessionV2
    ledger_event: dict[str, Any]
    metric_changes: tuple[MetricChangeV2, ...]
    route: dict[str, Any] | None
    route_status: str
    transition_contract: dict[str, Any] | None


class NumericV2Engine:
    """只在候选 Session 上应用数值、最少回合和作者路线。"""  # noqa: DOCSTRING_CJK

    def __init__(self, compiled: CompiledNumericV2Package):
        self.compiled = compiled
        self.story = compiled.story
        self.nodes = {str(node["id"]): node for node in self.story["nodes"]}
        self.metric_schema = self.story["metric_schema"]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "NumericV2Engine":
        return cls(NumericV2Compiler().compile(value))

    @property
    def story_id(self) -> str:
        return self.compiled.story_id

    def create_session(
        self,
        *,
        session_id: str,
        catgirl_binding: Mapping[str, Any],
        opening_performance: Mapping[str, Any],
    ) -> ScriptSessionV2:
        return ScriptSessionV2(
            session_id=_stable_id(session_id, "session_id"),
            story_package_id=self.story_id,
            story_package_revision=str(self.story["meta"]["revision"]),
            story_package_hash=self.compiled.package_hash,
            catgirl_binding={str(key): str(item) for key, item in catgirl_binding.items()},
            current_node_id=str(self.story["start_node_id"]),
            metrics={str(key): int(item) for key, item in self.story["initial_state"]["metrics"].items()},
            node_turn_count=0,
            revision=0,
            status="active",
            processed_client_turn_ids=(),
            opening_performance=deepcopy(dict(opening_performance)),
            performance_history=(),
            player_address_known=bool(self.story["initial_state"]["player_address_known"]),
        )

    def validate_session(self, session: ScriptSessionV2) -> None:
        if session.story_package_id != self.story_id:
            raise NumericV2RuntimeError("story_package_id_mismatch")
        if session.story_package_revision != str(self.story["meta"]["revision"]):
            raise NumericV2RuntimeError("story_package_revision_mismatch")
        if session.story_package_hash not in {
            self.compiled.package_hash,
            *self.compiled.compatible_package_hashes,
        }:
            raise NumericV2RuntimeError("story_package_hash_mismatch")
        if session.current_node_id not in self.nodes:
            raise NumericV2RuntimeError("session_current_node_missing")
        if session.status not in {"active", "ended"}:
            raise NumericV2RuntimeError("session_status_invalid")
        if set(session.metrics) != set(self.metric_schema):
            raise NumericV2RuntimeError("session_metrics_mismatch")
        for metric_id, value in session.metrics.items():
            definition = self.metric_schema[metric_id]
            if not definition["min"] <= value <= definition["max"]:
                raise NumericV2RuntimeError("session_metric_out_of_range")
        if session.node_turn_count < 0 or session.revision < 0:
            raise NumericV2RuntimeError("session_counter_invalid")
        if not isinstance(session.player_address_known, bool):
            raise NumericV2RuntimeError("session_player_address_known_invalid")
        self._validate_scene_goal_evidence(session, session.scene_goal_evidence)

    def _current_scene_evidence_revisions(self, session: ScriptSessionV2) -> set[int]:
        """返回当前节点最近一次访问中可被 Evaluator 引用的正式记录版本。"""  # noqa: DOCSTRING_CJK

        revisions: set[int] = set()
        entered_current_node = False
        current_node_id = str(session.current_node_id)
        for record in reversed(session.performance_history):
            from_node_id = str(record.get("from_node_id") or "")
            to_node_id = str(record.get("to_node_id") or "")
            if from_node_id == current_node_id and to_node_id == current_node_id:
                revision = record.get("revision")
                if isinstance(revision, int) and not isinstance(revision, bool):
                    revisions.add(revision)
                continue
            if to_node_id == current_node_id and from_node_id != current_node_id:
                revision = record.get("revision")
                if isinstance(revision, int) and not isinstance(revision, bool):
                    revisions.add(revision)
                entered_current_node = True
            break
        if not entered_current_node and current_node_id == str(self.story["start_node_id"]):
            revisions.add(0)
        return revisions

    def _validate_scene_goal_evidence(
        self,
        session: ScriptSessionV2,
        evidence: Mapping[str, tuple[int, ...]],
        *,
        pending_revision: int | None = None,
    ) -> None:
        """只接受当前节点目标和当前访问中的记录版本，防止跨幕事实串用。"""  # noqa: DOCSTRING_CJK

        # 新包直接校验作者目标 ID；旧包由兼容投影稳定得到 goal.N。
        goal_contracts = {
            str(goal["goal_id"]): goal
            for goal in numeric_v2_story_goal_contracts(
                self.nodes[session.current_node_id]["story_beat"]
            )
        }
        valid_goal_ids = set(goal_contracts)
        valid_revisions = self._current_scene_evidence_revisions(session)
        for goal_id, revisions in evidence.items():
            if goal_id not in valid_goal_ids:
                raise NumericV2RuntimeError("scene_goal_evidence_goal_invalid")
            if (
                not isinstance(revisions, tuple)
                or len(revisions) > 8
                or len(set(revisions)) != len(revisions)
            ):
                raise NumericV2RuntimeError("scene_goal_evidence_revision_invalid")
            allowed_revisions = set(valid_revisions)
            if (
                pending_revision is not None
                and str(goal_contracts[goal_id].get("owner") or "") == "player"
            ):
                allowed_revisions.add(pending_revision)
            if any(revision not in allowed_revisions for revision in revisions):
                raise NumericV2RuntimeError("scene_goal_evidence_revision_invalid")
        if len({revision for revisions in evidence.values() for revision in revisions}) > 8:
            raise NumericV2RuntimeError("scene_goal_evidence_revision_invalid")

    def resolve_turn(
        self,
        session: ScriptSessionV2,
        request: TurnRequestV2,
        changes: tuple[MetricChangeV2, ...],
        *,
        scene_complete: bool = False,
        goal_evidence: Mapping[str, tuple[int, ...]] | None = None,
        persist_scene_progress: bool = True,
    ) -> TurnOutcomeV2:
        self.validate_session(session)
        if session.status == "ended":
            raise NumericV2RuntimeError("session_already_ended")
        if request.base_revision != session.revision:
            raise NumericV2RevisionConflictError("base_revision_mismatch")
        if request.client_turn_id in session.processed_client_turn_ids:
            raise NumericV2DuplicateTurnError("duplicate_client_turn_id")
        if len({change.metric_id for change in changes}) != len(changes):
            raise NumericV2RuntimeError("metric_change_duplicate")

        submitted_goal_evidence = dict(goal_evidence or {})
        self._validate_scene_goal_evidence(
            session,
            submitted_goal_evidence,
            pending_revision=session.revision + 1,
        )
        merged_goal_evidence = {
            goal_id: tuple(dict.fromkeys((
                *session.scene_goal_evidence.get(goal_id, ()),
                *submitted_goal_evidence.get(goal_id, ()),
            )))[-4:]
            for goal_id in dict.fromkeys((
                *session.scene_goal_evidence,
                *submitted_goal_evidence,
            ))
        }
        # 目标证据只承担跨越最近窗口的短期保留职责。始终优先保存最近八个
        # 正式 revision，避免同一目标长期积累后反过来让合法 Session 无法复验。
        newest_evidence_revisions = set(sorted({
            revision
            for revisions in merged_goal_evidence.values()
            for revision in revisions
        })[-8:])
        merged_goal_evidence = {
            goal_id: tuple(
                revision
                for revision in revisions
                if revision in newest_evidence_revisions
            )
            for goal_id, revisions in merged_goal_evidence.items()
            if any(revision in newest_evidence_revisions for revision in revisions)
        }

        before = dict(session.metrics)
        after = dict(before)
        applied: list[dict[str, Any]] = []
        for change in changes:
            definition = self.metric_schema[change.metric_id]
            next_value = max(definition["min"], min(definition["max"], after[change.metric_id] + change.delta))
            applied.append({**change.to_dict(), "before": after[change.metric_id], "after": next_value})
            after[change.metric_id] = next_value

        source = self.nodes[session.current_node_id]
        next_turn_count = session.node_turn_count + 1
        route = None
        route_status = "waiting_min_turns"
        min_turns = int(source.get("min_turns") or 1)
        scene_completion_ready = (
            session.scene_completion_ready or scene_complete
            if persist_scene_progress
            else scene_complete
        )
        if next_turn_count >= min_turns and not scene_completion_ready:
            # min_turns 只是最短停留时间；本幕目标尚未兑现时不能按轮数硬切剧情。
            route_status = "scene_incomplete"
        elif next_turn_count >= min_turns:
            route, route_status = self._select_route(source, after)

        target_node_id = session.current_node_id
        next_status = "active"
        transition = None
        if route is not None:
            target_node_id = str(route["target_node_id"])
            target = self.nodes[target_node_id]
            next_turn_count = 0
            next_status = "ended" if target.get("type") == "ending" or target.get("terminal") is True else "active"
            transition = deepcopy(dict(route["transition_contract"]))
            route_status = "advanced"

        next_scene_completion_ready = scene_completion_ready if route is None else False
        # 完成信号已由 Runtime 锁存后，旧证据不再参与后续判定；即使仍在等待
        # min_turns，也只保留完成状态，避免反复占用 Evaluator 输入预算。
        next_scene_goal_evidence = (
            merged_goal_evidence
            if route is None and not scene_completion_ready
            else {}
        )
        player_address_known = _player_address_known_after_turn(session, request.message)

        revision = session.revision + 1
        next_session = replace(
            session,
            current_node_id=target_node_id,
            metrics=after,
            node_turn_count=next_turn_count,
            revision=revision,
            status=next_status,
            processed_client_turn_ids=(*session.processed_client_turn_ids, request.client_turn_id),
            player_address_known=player_address_known,
            scene_completion_ready=next_scene_completion_ready,
            scene_goal_evidence=next_scene_goal_evidence,
        )
        event = {
            "schema": LEDGER_EVENT_SCHEMA,
            "event_id": f"event_{session.session_id}_{revision}",
            "session_id": session.session_id,
            "client_turn_id": request.client_turn_id,
            "base_revision": session.revision,
            "result_revision": revision,
            "input_text": request.message,
            "metric_changes": applied,
            "before_metrics": before,
            "after_metrics": after,
            "from_node_id": session.current_node_id,
            "to_node_id": target_node_id,
            "route_id": route.get("id") if route else None,
            "route_status": route_status,
            "scene_complete": scene_complete,
            "node_turn_count": next_turn_count,
            "status": next_status,
            "player_address_known": player_address_known,
            # 版本 2 表示称呼状态由“完整昵称 + 明确披露句式”确定，旧事件按已提交事实兼容重放。
            "player_address_disclosure_version": 2,
        }
        if persist_scene_progress:
            # Ledger 保存累计状态，恢复时无需重新询问模型，也不会把旧幕证据带入新幕。
            event["scene_completion_ready"] = scene_completion_ready
            event["scene_goal_evidence"] = {
                goal_id: list(revisions)
                for goal_id, revisions in next_scene_goal_evidence.items()
            }
        return TurnOutcomeV2(next_session, event, changes, route, route_status, transition)

    def finalize_transition_performance(
        self,
        outcome: TurnOutcomeV2,
        performance: Mapping[str, Any],
        *,
        target_opening: str,
    ) -> dict[str, Any]:
        """由 Runtime 注入目标开场原文，模型只负责三段换场中的演绎内容。"""  # noqa: DOCSTRING_CJK

        target_node_id = str(outcome.ledger_event["to_node_id"])
        if outcome.ledger_event["from_node_id"] == target_node_id:
            return deepcopy(dict(performance))
        segments = performance.get("segments")
        if (
            not valid_scene_narration({"scene_narration": target_opening})
            or not isinstance(segments, list)
            or len(segments) != 3
            or not all(isinstance(item, Mapping) for item in segments)
            or [item.get("phase") for item in segments]
            != ["source_response", "transition_bridge", "target_opening"]
            or set(segments[0]) != {"phase", "performance"}
            or set(segments[1]) != {"phase", "scene_narration"}
            or set(segments[2]) != {"phase", "performance"}
            or not valid_mixed_performance(segments[0], require_dialogue=True)
            # 去重后没有独立过渡事实时允许空桥段，前端直接展示 Runtime 目标开场。
            or not valid_scene_narration(segments[1], allow_empty=True)
            or not valid_mixed_performance(segments[2], require_dialogue=True)
        ):
            raise NumericV2RuntimeError("numeric_transition_performance_invalid")
        result = deepcopy(dict(performance))
        result["segments"] = [
            deepcopy(dict(segments[0])),
            deepcopy(dict(segments[1])),
            {
                "phase": "target_opening",
                "scene_narration": target_opening.strip(),
                "performance": str(segments[2]["performance"]).strip(),
            },
        ]
        result["transition_delivered"] = True
        result["visible_node_id"] = target_node_id
        return result

    def _select_route(self, node: Mapping[str, Any], metrics: Mapping[str, int]) -> tuple[dict[str, Any] | None, str]:
        eligible = [route for route in node.get("route_gates", []) if self._conditions_match(route["conditions"], metrics)]
        if not eligible:
            return None, "conditions_blocked"
        highest = max(int(route["priority"]) for route in eligible)
        winners = [route for route in eligible if int(route["priority"]) == highest]
        if len(winners) != 1:
            raise NumericV2RuntimeError("route_priority_tie")
        return deepcopy(dict(winners[0])), "eligible"

    @staticmethod
    def _conditions_match(conditions: Mapping[str, Any], metrics: Mapping[str, int]) -> bool:
        mode = "any" if "any" in conditions else "all"
        rows = list(conditions.get(mode) or [])
        checks = [
            _COMPARATORS[str(row["op"])](metrics[str(row["metric"])], int(row["value"]))
            for row in rows
        ]
        return any(checks) if mode == "any" else all(checks)


class NumericV2Runtime:
    """组合 v2 Engine 和独立持久化目录。"""  # noqa: DOCSTRING_CJK

    def __init__(self, engine: NumericV2Engine, root: Path):
        self.engine = engine
        self.store = NumericV2SessionStore(Path(root), engine)

    async def start_session(
        self,
        *,
        session_id: str,
        catgirl_binding: Mapping[str, Any],
        opening_performance: Mapping[str, Any],
    ) -> NumericV2StoredSession:
        session = self.engine.create_session(
            session_id=session_id,
            catgirl_binding=catgirl_binding,
            opening_performance=opening_performance,
        )
        return await self.store.create_story_session(session)

    async def restore_story_session(
        self,
        catgirl_binding: Mapping[str, Any],
    ) -> NumericV2StoredSession | None:
        stored = await self.store.restore_story_session(
            self.engine.story_id,
            str(catgirl_binding.get("character_id") or ""),
            str(catgirl_binding.get("catgirl_name") or ""),
        )
        return await self._migrate_legacy_binding(stored, catgirl_binding)

    @asynccontextmanager
    async def story_session_guard(self):
        async with self.store.story_session_guard(self.engine.story_id):
            yield

    async def restore_story_session_unlocked(
        self,
        catgirl_binding: Mapping[str, Any],
    ) -> NumericV2StoredSession | None:
        stored = await self.store._restore_story_session_unlocked(
            self.engine.story_id,
            str(catgirl_binding.get("character_id") or ""),
            str(catgirl_binding.get("catgirl_name") or ""),
        )
        return await self._migrate_legacy_binding(stored, catgirl_binding)

    async def _migrate_legacy_binding(
        self,
        stored: NumericV2StoredSession | None,
        current_binding: Mapping[str, Any],
    ) -> NumericV2StoredSession | None:
        if stored is None or stored.session.catgirl_binding.get("character_id"):
            return stored
        legacy_expected = {
            str(key): str(value)
            for key, value in current_binding.items()
            if key != "character_id"
        }
        legacy_expected["catgirl_id"] = (
            f"catgirl:{current_binding.get('catgirl_name') or ''}"
        )
        if stored.session.catgirl_binding != legacy_expected:
            return stored
        migrated = await self.store.update_catgirl_binding(
            stored.session.session_id,
            current_binding,
        )
        await self.store.set_story_session_id(
            self.engine.story_id,
            str(current_binding.get("character_id") or ""),
            migrated.session.session_id,
        )
        return migrated

    async def replace_active_session(
        self,
        *,
        previous_session_id: str,
        session_id: str,
        catgirl_binding: Mapping[str, Any],
        opening_performance: Mapping[str, Any],
    ) -> NumericV2StoredSession:
        session = self.engine.create_session(
            session_id=session_id,
            catgirl_binding=catgirl_binding,
            opening_performance=opening_performance,
        )
        stored = await self.store.replace_active(previous_session_id, session)
        return stored

    async def restore_session(self, session_id: str) -> NumericV2StoredSession | None:
        return await self.store.load(session_id)

    def prepare_turn(
        self,
        current: NumericV2StoredSession,
        request: TurnRequestV2,
        changes: tuple[MetricChangeV2, ...],
        *,
        scene_complete: bool = False,
        goal_evidence: Mapping[str, tuple[int, ...]] | None = None,
    ) -> TurnOutcomeV2:
        return self.engine.resolve_turn(
            current.session,
            request,
            changes,
            scene_complete=scene_complete,
            goal_evidence=goal_evidence,
        )

    async def commit_turn(
        self,
        outcome: TurnOutcomeV2,
        performance: Mapping[str, Any],
    ) -> NumericV2StoredSession:
        route_changed = outcome.ledger_event["from_node_id"] != outcome.ledger_event["to_node_id"]
        if route_changed:
            segments = performance.get("segments")
            new_contract = (
                isinstance(segments, list)
                and bool(segments)
                and isinstance(segments[0], Mapping)
                and "performance" in segments[0]
            )
            if (
                performance.get("transition_delivered") is not True
                or performance.get("visible_node_id") != outcome.ledger_event["to_node_id"]
                or not isinstance(segments, list)
                or [item.get("phase") for item in segments if isinstance(item, Mapping)]
                != ["source_response", "transition_bridge", "target_opening"]
                or (
                    new_contract
                    and (
                        set(segments[0]) != {"phase", "performance"}
                        or set(segments[1]) != {"phase", "scene_narration"}
                        or set(segments[2]) != {"phase", "scene_narration", "performance"}
                        or not valid_mixed_performance(segments[0], require_dialogue=True)
                        or not valid_scene_narration(segments[1], allow_empty=True)
                        or not valid_scene_narration(segments[2])
                        or not valid_mixed_performance(segments[2], require_dialogue=True)
                    )
                )
                or (
                    not new_contract
                    and (
                        not valid_ordered_content(segments[0], require_dialogue=True)
                        or not valid_ordered_content(segments[1], require_narration=True)
                        or not valid_ordered_content(segments[2], require_narration=True)
                    )
                )
            ):
                raise NumericV2RuntimeError("numeric_transition_performance_invalid")
        elif "performance" in performance and not valid_mixed_performance(
            performance,
            require_dialogue=True,
        ):
            raise NumericV2RuntimeError("numeric_performance_invalid")
        new_contract = "performance" in performance or (
            route_changed
            and isinstance(performance.get("segments"), list)
            and any(
                isinstance(segment, Mapping)
                and ("performance" in segment or "scene_narration" in segment)
                for segment in performance["segments"]
            )
        )
        record = {
            "schema": PERFORMANCE_RECORD_SCHEMA,
            "client_turn_id": outcome.ledger_event["client_turn_id"],
            "revision": outcome.session.revision,
            "input_text": outcome.ledger_event["input_text"],
            "from_node_id": outcome.ledger_event["from_node_id"],
            "to_node_id": outcome.ledger_event["to_node_id"],
            **deepcopy(dict(performance)),
            # 旧记录没有该标记；版本 3 才强制混合正文合同，保证历史 Session 可恢复。
            "performance_contract_version": (
                3
                if new_contract
                else (2 if "content" in performance or route_changed else 1)
            ),
        }
        session = replace(
            outcome.session,
            performance_history=(*outcome.session.performance_history, record),
        )
        return await self.store.commit(session, outcome.ledger_event)

    async def end_session(self, session_id: str, *, base_revision: int, reason: str) -> NumericV2StoredSession:
        return await self.store.end_session(session_id, base_revision=base_revision, reason=reason)

    async def resume_session(self, session_id: str, *, base_revision: int) -> NumericV2StoredSession:
        return await self.store.resume_session(session_id, base_revision=base_revision)


__all__ = [
    "LEDGER_EVENT_SCHEMA",
    "MetricChangeV2",
    "NumericV2DuplicateTurnError",
    "NumericV2Engine",
    "NumericV2RevisionConflictError",
    "NumericV2Runtime",
    "NumericV2RuntimeError",
    "PERFORMANCE_RECORD_SCHEMA",
    "SESSION_SCHEMA",
    "ScriptSessionV2",
    "TurnOutcomeV2",
    "TurnRequestV2",
]
