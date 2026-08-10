"""Numeric v2 的确定性状态引擎与 Runtime 入口。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import re
from pathlib import Path
from typing import Any, Mapping

from .numeric_v2 import CompiledNumericV2Package, NumericV2Compiler
from .numeric_v2_store import (
    NumericV2SessionNotFoundError,
    NumericV2SessionStore,
    NumericV2StoredSession,
)


SESSION_SCHEMA = "neko.script.session.numeric.v2"
LEDGER_EVENT_SCHEMA = "neko.script.ledger_event.numeric.v2"
PERFORMANCE_RECORD_SCHEMA = "neko.script.performance_record.numeric.v2"
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
    """Numeric v2 回合无法在当前确定性状态上结算。"""


class NumericV2RevisionConflictError(NumericV2RuntimeError):
    """客户端基于过期 revision 提交。"""


class NumericV2DuplicateTurnError(NumericV2RuntimeError):
    """同一个 client_turn_id 已经成功提交。"""


def _stable_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise NumericV2RuntimeError(f"{field}_invalid")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NumericV2RuntimeError(f"{field}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class MetricChangeV2:
    """判定模型提出、确定性引擎复验后的单项数值变化。"""

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
    ended_reason: str | None = None

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
            "ended_reason": self.ended_reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScriptSessionV2":
        if value.get("schema") != SESSION_SCHEMA:
            raise NumericV2RuntimeError("numeric_session_schema_invalid")
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
            ended_reason=(str(value.get("ended_reason")) if value.get("ended_reason") is not None else None),
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
    """只在候选 Session 上应用数值、最少回合和作者路线。"""

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
        )

    def validate_session(self, session: ScriptSessionV2) -> None:
        if session.story_package_id != self.story_id:
            raise NumericV2RuntimeError("story_package_id_mismatch")
        if session.story_package_revision != str(self.story["meta"]["revision"]):
            raise NumericV2RuntimeError("story_package_revision_mismatch")
        if session.story_package_hash != self.compiled.package_hash:
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

    def resolve_turn(
        self,
        session: ScriptSessionV2,
        request: TurnRequestV2,
        changes: tuple[MetricChangeV2, ...],
        *,
        scene_complete: bool = False,
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
        if next_turn_count >= min_turns and not scene_complete:
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

        revision = session.revision + 1
        next_session = replace(
            session,
            current_node_id=target_node_id,
            metrics=after,
            node_turn_count=next_turn_count,
            revision=revision,
            status=next_status,
            processed_client_turn_ids=(*session.processed_client_turn_ids, request.client_turn_id),
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
        }
        return TurnOutcomeV2(next_session, event, changes, route, route_status, transition)

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
    """组合 v2 Engine 和独立持久化目录。"""

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
        return await self.store.create(session)

    async def restore_session(self, session_id: str) -> NumericV2StoredSession | None:
        return await self.store.load(session_id)

    def prepare_turn(
        self,
        current: NumericV2StoredSession,
        request: TurnRequestV2,
        changes: tuple[MetricChangeV2, ...],
        *,
        scene_complete: bool = False,
    ) -> TurnOutcomeV2:
        return self.engine.resolve_turn(
            current.session,
            request,
            changes,
            scene_complete=scene_complete,
        )

    async def commit_turn(
        self,
        outcome: TurnOutcomeV2,
        performance: Mapping[str, Any],
    ) -> NumericV2StoredSession:
        record = {
            "schema": PERFORMANCE_RECORD_SCHEMA,
            "client_turn_id": outcome.ledger_event["client_turn_id"],
            "revision": outcome.session.revision,
            "input_text": outcome.ledger_event["input_text"],
            "from_node_id": outcome.ledger_event["from_node_id"],
            "to_node_id": outcome.ledger_event["to_node_id"],
            **deepcopy(dict(performance)),
        }
        session = replace(
            outcome.session,
            performance_history=(*outcome.session.performance_history, record),
        )
        return await self.store.commit(session, outcome.ledger_event)

    async def end_session(self, session_id: str, *, base_revision: int, reason: str) -> NumericV2StoredSession:
        return await self.store.end_session(session_id, base_revision=base_revision, reason=reason)


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
