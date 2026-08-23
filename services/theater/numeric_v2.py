"""Numeric v2 Story Package 的确定性编译边界。

本模块只负责作者包合同、规范化字节和静态可达性，不实现 Actor、Session、
Ledger 或回合数值判定。InkAI 发布前会调用这里进行第二次独立复验。
"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping


STORY_SCHEMA = "neko.story.numeric.v2"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMPARATORS = frozenset({"==", "!=", ">", "<", ">=", "<="})
_VISIBILITIES = frozenset({"hidden"})
_NODE_TYPES = frozenset({"start", "scene", "ending"})
MAX_RECOMMENDED_TURNS = 40
_FORBIDDEN_LEGACY_FIELDS = frozenset(
    {"interaction_rules", "available_interaction_ids", "choices", "state_schema"}
)
_IDENTITY_SEPARATOR_RE = re.compile(r"[，,；;：:\n（(]")
_PLAYER_OWNED_SUBJECT_RE = re.compile(
    r"(?:^|[，,。！？；;：:])\s*(?:为了[^，,；;]{0,20}[，,]\s*)?"
    r"(?:在|由|当|随着|经过|根据)?\s*(?:你|您|玩家|男主|哥哥|他|你们|双方|两人|共同)(?!的)"
)
_FORCED_PLAYER_ACTION_RE = re.compile(
    r"(?:女主|猫娘|她).{0,20}(?:强迫|逼迫|迫使|强制|命令).{0,20}"
    r"(?:你|您|玩家|男主|哥哥|他)"
)


@dataclass(frozen=True)
class NumericV2Issue:
    """一次返回给作者的稳定合同问题。"""  # noqa: DOCSTRING_CJK

    code: str
    path: str
    message: str


@dataclass(frozen=True)
class NumericV2Warning:
    """不阻止包复验的静态质量提示。"""  # noqa: DOCSTRING_CJK

    code: str
    path: str
    message: str


class NumericV2CompileError(ValueError):
    """Numeric v2 包含一个或多个硬合同问题。"""  # noqa: DOCSTRING_CJK

    def __init__(self, issues: list[NumericV2Issue]):
        super().__init__("numeric_v2_compile_failed")
        self.issues = tuple(issues)


@dataclass(frozen=True)
class CompiledNumericV2Package:
    """已通过 N.E.K.O 复验的 canonical Story Package。"""  # noqa: DOCSTRING_CJK

    story: dict[str, Any]
    json_bytes: bytes
    package_hash: str
    warnings: tuple[NumericV2Warning, ...]

    @property
    def story_id(self) -> str:
        return str(self.story["meta"]["story_id"])


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _identity_source_name(value: Any) -> str:
    return _IDENTITY_SEPARATOR_RE.split(str(value or "").strip(), maxsplit=1)[0].strip()


def _first_sentence(value: Any) -> str:
    """只检查会被 Runtime 确定性交付的节点开场句。"""  # noqa: DOCSTRING_CJK

    text = str(value or "").strip()
    endings = [index for mark in "。！？" if (index := text.find(mark)) >= 0]
    return text[:min(endings) + 1] if endings else text


def _condition_branches(conditions: Mapping[str, Any]) -> list[list[Mapping[str, Any]]]:
    if not isinstance(conditions, Mapping):
        return []
    mode = "any" if "any" in conditions else "all"
    rows = [row for row in conditions.get(mode) or [] if isinstance(row, Mapping)]
    return [rows] if mode == "all" else [[row] for row in rows]


def _conditions_overlap(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    metric_ranges: Mapping[str, tuple[int | None, int | None]],
) -> bool:
    """Return whether two route predicates can be true for one metric state."""

    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    for left_branch in _condition_branches(left):
        for right_branch in _condition_branches(right):
            rows = [*left_branch, *right_branch]
            by_metric: dict[str, list[Mapping[str, Any]]] = {}
            for row in rows:
                metric = str(row.get("metric") or "")
                if metric not in metric_ranges or row.get("op") not in _COMPARATORS or not _is_int(row.get("value")):
                    # Other validation reports the malformed condition; do not
                    # add a secondary overlap error for the same malformed row.
                    by_metric = {}
                    break
                by_metric.setdefault(metric, []).append(row)
            if not by_metric and rows:
                continue
            possible = True
            for metric, metric_rows in by_metric.items():
                minimum, maximum = metric_ranges[metric]
                if minimum is None or maximum is None:
                    possible = False
                    break
                candidates = {minimum, maximum}
                for row in metric_rows:
                    threshold = int(row["value"])
                    candidates.update({threshold - 1, threshold, threshold + 1})
                if not any(
                    minimum <= candidate <= maximum
                    and all(
                        {
                            "==": candidate == int(row["value"]),
                            "!=": candidate != int(row["value"]),
                            ">": candidate > int(row["value"]),
                            "<": candidate < int(row["value"]),
                            ">=": candidate >= int(row["value"]),
                            "<=": candidate <= int(row["value"]),
                        }[str(row["op"])]
                        for row in metric_rows
                    )
                    for candidate in candidates
                ):
                    possible = False
                    break
            if possible:
                return True
    return False


class _Collector:
    """集中收集问题，避免遇到第一个错误就中断作者定位。"""  # noqa: DOCSTRING_CJK

    def __init__(self) -> None:
        self.issues: list[NumericV2Issue] = []

    def add(self, code: str, path: str, message: str) -> None:
        self.issues.append(NumericV2Issue(code, path, message))

    def obj(self, value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            self.add("expected_object", path, "必须是对象。")
            return {}
        return dict(value)

    def array(self, value: Any, path: str) -> list[Any]:
        if not isinstance(value, list):
            self.add("expected_array", path, "必须是数组。")
            return []
        return value

    def require_text(self, value: Any, path: str) -> str:
        if not _text(value):
            self.add("required_text", path, "必须填写非空文本。")
            return ""
        return str(value)

    def require_id(self, value: Any, path: str) -> str:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            self.add("invalid_id", path, "必须是安全且稳定的 ID。")
            return ""
        return value

    def require_int(self, value: Any, path: str) -> int | None:
        if not _is_int(value):
            self.add("expected_integer", path, "必须是整数。")
            return None
        return int(value)

    def require_text_list(self, value: Any, path: str, *, allow_empty: bool) -> list[str]:
        items = self.array(value, path)
        if not allow_empty and not items:
            self.add("required_items", path, "至少需要填写一项。")
        result: list[str] = []
        for index, item in enumerate(items):
            text = self.require_text(item, f"{path}[{index}]")
            if text:
                result.append(text)
        return result


class NumericV2Compiler:
    """只编译 Numeric v2 作者包，不提供旧协议兼容或迁移。"""  # noqa: DOCSTRING_CJK

    def compile(self, payload: Mapping[str, Any]) -> CompiledNumericV2Package:
        collector = _Collector()
        story = collector.obj(payload, "story")
        if story.get("schema") != STORY_SCHEMA:
            collector.add("invalid_schema", "schema", f"schema 必须是 {STORY_SCHEMA}。")
        for field in sorted(_FORBIDDEN_LEGACY_FIELDS.intersection(story)):
            collector.add("legacy_field_forbidden", field, "Numeric v2 不允许包含旧协议字段。")

        self._validate_meta(collector, story.get("meta"))
        self._validate_intro(collector, story.get("intro"))
        collector.obj(story.get("characters"), "characters")
        self._validate_binding(collector, story.get("catgirl_binding"))
        metric_ranges = self._validate_metrics(collector, story.get("metric_schema"), story.get("initial_state"))
        nodes, route_targets = self._validate_nodes(collector, story.get("nodes"), metric_ranges)
        ending_ids = self._validate_endings(collector, story.get("endings"))
        self._validate_graph(
            collector,
            start_node_id=story.get("start_node_id"),
            nodes=nodes,
            route_targets=route_targets,
            ending_ids=ending_ids,
        )
        if collector.issues:
            raise NumericV2CompileError(collector.issues)

        canonical_story = deepcopy(story)
        canonical_bytes = json.dumps(
            canonical_story,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        warnings = self._warnings(canonical_story)
        return CompiledNumericV2Package(
            story=canonical_story,
            json_bytes=canonical_bytes,
            package_hash=f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}",
            warnings=tuple(warnings),
        )

    @staticmethod
    def _validate_meta(c: _Collector, value: Any) -> None:
        meta = c.obj(value, "meta")
        c.require_id(meta.get("story_id"), "meta.story_id")
        for field in ("title", "author", "revision", "language"):
            c.require_text(meta.get(field), f"meta.{field}")

    @staticmethod
    def _validate_intro(c: _Collector, value: Any) -> None:
        intro = c.obj(value, "intro")
        for field in ("background", "player_identity", "catgirl_identity"):
            c.require_text(intro.get(field), f"intro.{field}")
        player_name = _identity_source_name(intro.get("player_identity"))
        catgirl_name = _identity_source_name(intro.get("catgirl_identity"))
        if player_name and player_name == catgirl_name:
            c.add(
                "intro_identity_names_conflict",
                "intro",
                "玩家与猫娘身份的首段名称不能相同。",
            )

    @staticmethod
    def _validate_binding(c: _Collector, value: Any) -> None:
        binding = c.obj(value, "catgirl_binding")
        if binding.get("source") != "runtime.current_catgirl":
            c.add("invalid_catgirl_binding", "catgirl_binding.source", "必须绑定当前猫娘配置。")
        c.require_text(binding.get("role_overlay"), "catgirl_binding.role_overlay")

    @staticmethod
    def _validate_metrics(c: _Collector, value: Any, initial_value: Any) -> dict[str, tuple[int | None, int | None]]:
        metrics = c.obj(value, "metric_schema")
        if len(metrics) > 4:
            c.add("metric_limit_exceeded", "metric_schema", "第一版最多启用四个 metric。")
        metric_ranges: dict[str, tuple[int | None, int | None]] = {}
        for metric_id, raw in metrics.items():
            path = f"metric_schema.{metric_id}"
            stable_id = c.require_id(metric_id, path)
            metric = c.obj(raw, path)
            c.require_text(metric.get("name"), f"{path}.name")
            c.require_text(metric.get("description"), f"{path}.description")
            minimum = c.require_int(metric.get("min"), f"{path}.min")
            maximum = c.require_int(metric.get("max"), f"{path}.max")
            initial = c.require_int(metric.get("initial"), f"{path}.initial")
            if stable_id:
                metric_ranges[stable_id] = (minimum, maximum)
            if minimum is not None and maximum is not None and minimum >= maximum:
                c.add("invalid_metric_range", path, "metric 的最小值必须小于最大值。")
            if None not in (minimum, maximum, initial) and not minimum <= initial <= maximum:
                c.add("metric_initial_out_of_range", f"{path}.initial", "初始值超出 metric 范围。")
            if metric.get("visibility") not in _VISIBILITIES:
                c.add("invalid_metric_visibility", f"{path}.visibility", "Numeric v2 数值只能对玩家隐藏。")
            if metric.get("relationship_effect", "none") not in {"positive", "negative", "none"}:
                c.add(
                    "invalid_metric_relationship_effect",
                    f"{path}.relationship_effect",
                    "关系演绎作用只能是 positive、negative 或 none。",
                )
            limits = c.obj(metric.get("per_turn_limit"), f"{path}.per_turn_limit")
            for direction in ("increase", "decrease"):
                limit = c.require_int(limits.get(direction), f"{path}.per_turn_limit.{direction}")
                if limit is not None and limit <= 0:
                    c.add("invalid_turn_limit", f"{path}.per_turn_limit.{direction}", "单回合限幅必须为正整数。")
            increase = c.require_text_list(metric.get("increase_criteria"), f"{path}.increase_criteria", allow_empty=False)
            decrease = c.require_text_list(metric.get("decrease_criteria"), f"{path}.decrease_criteria", allow_empty=False)
            if increase and decrease and set(increase) == set(decrease):
                c.add("metric_criteria_overlap", path, "增加依据和减少依据不能完全相同。")
            NumericV2Compiler._validate_bands(c, metric.get("bands"), path, minimum, maximum)

        initial = c.obj(initial_value, "initial_state")
        initial_metrics = c.obj(initial.get("metrics"), "initial_state.metrics")
        if set(initial_metrics) != set(metric_ranges):
            c.add("initial_metrics_mismatch", "initial_state.metrics", "初始状态必须精确覆盖全部 metric。")
        for metric_id, value in initial_metrics.items():
            numeric = c.require_int(value, f"initial_state.metrics.{metric_id}")
            definition = metrics.get(metric_id)
            if numeric is not None and isinstance(definition, Mapping):
                minimum = definition.get("min")
                maximum = definition.get("max")
                if _is_int(minimum) and _is_int(maximum) and not minimum <= numeric <= maximum:
                    c.add("initial_metric_out_of_range", f"initial_state.metrics.{metric_id}", "初始状态数值越界。")
                declared_initial = definition.get("initial")
                if _is_int(declared_initial) and numeric != declared_initial:
                    c.add(
                        "initial_metric_value_mismatch",
                        f"initial_state.metrics.{metric_id}",
                        "初始状态数值必须与 metric_schema 的 initial 一致。",
                    )
        return metric_ranges

    @staticmethod
    def _validate_bands(c: _Collector, value: Any, path: str, minimum: int | None, maximum: int | None) -> None:
        bands = c.array(value, f"{path}.bands")
        parsed: list[tuple[int, int]] = []
        for index, raw in enumerate(bands):
            band_path = f"{path}.bands[{index}]"
            band = c.obj(raw, band_path)
            start = c.require_int(band.get("min"), f"{band_path}.min")
            end = c.require_int(band.get("max"), f"{band_path}.max")
            c.require_text(band.get("label"), f"{band_path}.label")
            if start is not None and end is not None:
                if start > end:
                    c.add("invalid_metric_band", band_path, "区间起点不能大于终点。")
                parsed.append((start, end))
        if minimum is None or maximum is None or not parsed:
            if not parsed:
                c.add("metric_bands_required", f"{path}.bands", "bands 必须完整覆盖 metric 范围。")
            return
        parsed.sort()
        cursor = minimum
        for start, end in parsed:
            if start != cursor:
                c.add("metric_bands_not_contiguous", f"{path}.bands", "bands 存在重叠、断档或越界。")
                return
            cursor = end + 1
        if cursor != maximum + 1:
            c.add("metric_bands_not_contiguous", f"{path}.bands", "bands 必须完整覆盖 metric 范围。")

    @staticmethod
    def _validate_story_beat(c: _Collector, value: Any, path: str) -> None:
        beat = c.obj(value, path)
        summary = c.require_text(beat.get("summary"), f"{path}.summary")
        if summary and _PLAYER_OWNED_SUBJECT_RE.search(_first_sentence(summary)):
            c.add(
                "player_owned_opening_forbidden",
                f"{path}.summary",
                "节点开场只能建立环境或猫娘可见行动，不得替玩家执行行动或决定。",
            )
        must_happen = c.require_text_list(
            beat.get("must_happen"),
            f"{path}.must_happen",
            allow_empty=False,
        )
        for index, item in enumerate(must_happen):
            if (
                _PLAYER_OWNED_SUBJECT_RE.search(item)
                or _FORCED_PLAYER_ACTION_RE.search(item)
            ):
                c.add(
                    "player_owned_goal_forbidden",
                    f"{path}.must_happen[{index}]",
                    "幕目标必须由猫娘或环境主动呈现，不得预先规定玩家行动。",
                )
        c.require_text_list(beat.get("must_not_happen"), f"{path}.must_not_happen", allow_empty=True)
        c.require_text(beat.get("catgirl_situation"), f"{path}.catgirl_situation")
        c.require_text(beat.get("transition_goal"), f"{path}.transition_goal")

    @staticmethod
    def _validate_nodes(c: _Collector, value: Any, metric_ranges: Mapping[str, tuple[int | None, int | None]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
        nodes: dict[str, dict[str, Any]] = {}
        route_targets: dict[str, list[str]] = {}
        route_ids: set[str] = set()
        for index, raw in enumerate(c.array(value, "nodes")):
            path = f"nodes[{index}]"
            node = c.obj(raw, path)
            node_id = c.require_id(node.get("id"), f"{path}.id")
            if node_id in nodes:
                c.add("duplicate_node_id", f"{path}.id", "节点 ID 重复。")
            if node_id:
                nodes[node_id] = node
            if node.get("type") not in _NODE_TYPES:
                c.add("invalid_node_type", f"{path}.type", "节点类型必须是 start、scene 或 ending。")
            c.require_text(node.get("chapter"), f"{path}.chapter")
            if node.get("type") != "ending":
                min_turns = c.require_int(node.get("min_turns"), f"{path}.min_turns")
                if min_turns is not None and not 1 <= min_turns <= 20:
                    c.add("invalid_node_min_turns", f"{path}.min_turns", "最少演绎回合数必须位于 1..20。")
                if "recommended_turns" in node:
                    recommended_turns = c.require_int(
                        node.get("recommended_turns"),
                        f"{path}.recommended_turns",
                    )
                    if (
                        recommended_turns is not None
                        and (
                            min_turns is None
                            or recommended_turns < min_turns
                            or recommended_turns > MAX_RECOMMENDED_TURNS
                        )
                    ):
                        c.add(
                            "invalid_node_recommended_turns",
                            f"{path}.recommended_turns",
                            f"建议收束回合数必须不小于 min_turns，且不能超过 {MAX_RECOMMENDED_TURNS}。",
                        )
            NumericV2Compiler._validate_story_beat(c, node.get("story_beat"), f"{path}.story_beat")
            if any(field in node for field in ("choices", "available_interaction_ids", "edges")):
                c.add("legacy_node_field_forbidden", path, "Numeric v2 节点不能包含旧 Choice、interaction 或 Edge 字段。")
            routes = c.array(node.get("route_gates"), f"{path}.route_gates")
            # 单出口表示确定性的顺序推进，不要求作者为了主线编译虚构数值条件；
            # 同一节点出现多个出口时，所有路线仍必须用作者配置的数值明确区分。
            allow_empty_conditions = len(routes) == 1
            targets: list[str] = []
            priorities: dict[int, list[tuple[str, dict[str, Any], str]]] = {}
            for route_index, route_raw in enumerate(routes):
                route_path = f"{path}.route_gates[{route_index}]"
                route = c.obj(route_raw, route_path)
                route_id = c.require_id(route.get("id"), f"{route_path}.id")
                if route_id in route_ids:
                    c.add("duplicate_route_id", f"{route_path}.id", "路线 ID 重复。")
                if route_id:
                    route_ids.add(route_id)
                target = c.require_id(route.get("target_node_id"), f"{route_path}.target_node_id")
                if target:
                    targets.append(target)
                priority = c.require_int(route.get("priority"), f"{route_path}.priority")
                signature = NumericV2Compiler._validate_conditions(
                    c,
                    route.get("conditions"),
                    route_path,
                    metric_ranges,
                    allow_empty=allow_empty_conditions,
                )
                if priority is not None:
                    same_priority = priorities.setdefault(priority, [])
                    raw_conditions = route.get("conditions")
                    conditions = raw_conditions if isinstance(raw_conditions, Mapping) else {}
                    if any(existing_signature == signature for existing_signature, _conditions, _path in same_priority):
                        c.add("duplicate_route_priority", route_path, "同一来源节点存在同优先级且相同条件的路线。")
                    elif any(
                        _conditions_overlap(existing_conditions, conditions, metric_ranges)
                        for existing_signature, existing_conditions, _path in same_priority
                        if existing_signature != "invalid" and signature != "invalid"
                    ):
                        c.add("overlapping_route_priority", route_path, "同一来源节点存在同优先级且条件重叠的路线。")
                    same_priority.append((signature, dict(conditions), route_path))
                NumericV2Compiler._validate_transition(c, route.get("transition_contract"), f"{route_path}.transition_contract")
            if node_id:
                route_targets[node_id] = targets
            terminal = node.get("type") == "ending" or node.get("terminal") is True
            if terminal:
                if routes:
                    c.add("terminal_route_forbidden", f"{path}.route_gates", "结局节点不能创建出边。")
                c.require_id(node.get("ending_id"), f"{path}.ending_id")
            elif not routes:
                c.add("node_route_required", f"{path}.route_gates", "幕节点不能作为路线终点，至少需要一条通往后续幕或结局的路线。")
        return nodes, route_targets

    @staticmethod
    def _validate_conditions(
        c: _Collector,
        value: Any,
        route_path: str,
        metric_ranges: Mapping[str, tuple[int | None, int | None]],
        *,
        allow_empty: bool,
    ) -> str:
        conditions = c.obj(value, f"{route_path}.conditions")
        modes = [mode for mode in ("all", "any") if mode in conditions]
        if len(modes) != 1:
            c.add("invalid_condition_mode", f"{route_path}.conditions", "条件必须且只能使用 all 或 any。")
            return "invalid"
        mode = modes[0]
        rows = c.array(conditions.get(mode), f"{route_path}.conditions.{mode}")
        if not rows and (not allow_empty or mode == "any"):
            c.add("route_condition_required", f"{route_path}.conditions.{mode}", "同一幕存在多个出口时，每条路线至少需要一个 metric 条件。")
        signature_rows: list[str] = []
        can_check_compound = True
        for index, raw in enumerate(rows):
            path = f"{route_path}.conditions.{mode}[{index}]"
            condition = c.obj(raw, path)
            if condition.get("type") != "metric_compare":
                c.add("invalid_condition_type", f"{path}.type", "第一版只支持 metric_compare。")
                can_check_compound = False
            metric = c.require_id(condition.get("metric"), f"{path}.metric")
            if not metric or metric not in metric_ranges:
                can_check_compound = False
            if metric and metric not in metric_ranges:
                c.add("unknown_metric", f"{path}.metric", "路线引用了未知 metric。")
            operator = condition.get("op")
            if operator not in _COMPARATORS:
                c.add("invalid_metric_comparator", f"{path}.op", "不支持该比较符。")
                can_check_compound = False
            number = c.require_int(condition.get("value"), f"{path}.value")
            if number is None:
                can_check_compound = False
            if metric in metric_ranges and number is not None:
                minimum, maximum = metric_ranges[metric]
                if minimum is not None and maximum is not None and not minimum <= number <= maximum:
                    c.add("route_threshold_out_of_range", f"{path}.value", "路线阈值超出 metric 范围。")
                    can_check_compound = False
                elif (
                    minimum is not None
                    and maximum is not None
                    and operator in _COMPARATORS
                    and not {
                        "==": minimum <= number <= maximum,
                        "!=": minimum < maximum,
                        ">": maximum > number,
                        "<": minimum < number,
                        ">=": maximum >= number,
                        "<=": minimum <= number,
                    }[operator]
                ):
                    c.add(
                        "route_condition_impossible",
                        f"{path}.value",
                        "路线条件在 metric 的声明范围内永远无法成立。",
                    )
            signature_rows.append(f"{metric}:{operator}:{number}")
        if len(rows) > 1 and can_check_compound:
            if not _conditions_overlap(conditions, {"all": []}, metric_ranges):
                c.add(
                    "route_condition_impossible",
                    f"{route_path}.conditions",
                    "路线条件组合在 metric 的声明范围内永远无法同时成立。",
                )
        return f"{mode}|{'|'.join(sorted(signature_rows))}"

    @staticmethod
    def _validate_transition(c: _Collector, value: Any, path: str) -> None:
        contract = c.obj(value, path)
        c.require_text(contract.get("reason"), f"{path}.reason")
        must_deliver = c.require_text_list(
            contract.get("must_deliver"),
            f"{path}.must_deliver",
            allow_empty=False,
        )
        for index, item in enumerate(must_deliver):
            if (
                _PLAYER_OWNED_SUBJECT_RE.search(item)
                or _FORCED_PLAYER_ACTION_RE.search(item)
            ):
                c.add(
                    "player_owned_transition_forbidden",
                    f"{path}.must_deliver[{index}]",
                    "过渡合同不得把玩家尚未执行的行动写成必须交付的事实。",
                )
        c.require_text_list(contract.get("must_preserve"), f"{path}.must_preserve", allow_empty=True)
        c.require_text(contract.get("tone"), f"{path}.tone")

    @staticmethod
    def _validate_endings(c: _Collector, value: Any) -> set[str]:
        ids: set[str] = set()
        for index, raw in enumerate(c.array(value, "endings")):
            path = f"endings[{index}]"
            ending = c.obj(raw, path)
            ending_id = c.require_id(ending.get("id"), f"{path}.id")
            if ending_id in ids:
                c.add("duplicate_ending_id", f"{path}.id", "结局 ID 重复。")
            if ending_id:
                ids.add(ending_id)
            c.require_text(ending.get("title"), f"{path}.title")
            c.require_text(ending.get("summary"), f"{path}.summary")
            if ending.get("terminal") is not True:
                c.add("ending_not_terminal", f"{path}.terminal", "结局必须标记 terminal=true。")
        if not ids:
            c.add("ending_required", "endings", "至少需要一个结局。")
        return ids

    @staticmethod
    def _validate_graph(c: _Collector, *, start_node_id: Any, nodes: dict[str, dict[str, Any]], route_targets: dict[str, list[str]], ending_ids: set[str]) -> None:
        start = c.require_id(start_node_id, "start_node_id")
        if start and start not in nodes:
            c.add("unknown_start_node", "start_node_id", "开场节点不存在。")
        elif start and nodes[start].get("type") != "start":
            c.add("invalid_start_node_type", "start_node_id", "start_node_id 必须指向 start 节点。")
        for source, targets in route_targets.items():
            for target in targets:
                if target not in nodes:
                    c.add("unknown_target_node", f"nodes.{source}.route_gates", f"目标节点 {target} 不存在。")
        terminal_nodes = {
            node_id: node for node_id, node in nodes.items()
            if node.get("type") == "ending" or node.get("terminal") is True
        }
        for node_id, node in terminal_nodes.items():
            if node.get("ending_id") not in ending_ids:
                c.add("unknown_ending", f"nodes.{node_id}.ending_id", "结局节点引用了未知 ending。")
        if not start or start not in nodes:
            return
        reachable: set[str] = set()
        stack = [start]
        while stack:
            node_id = stack.pop()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            stack.extend(target for target in route_targets.get(node_id, []) if target in nodes)
        for node_id in sorted(set(nodes).difference(reachable)):
            c.add("unreachable_node", f"nodes.{node_id}", "节点从开场不可达。")
        reached_endings = {
            node.get("ending_id") for node_id, node in terminal_nodes.items() if node_id in reachable
        }
        for ending_id in sorted(ending_ids.difference(reached_endings)):
            c.add("unreachable_ending", f"endings.{ending_id}", "结局从开场不可达。")

        # 从所有结局反向遍历，保证每条可达支线都能收束到结局，而不是停在幕节点或循环中。
        reverse_routes: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for source, targets in route_targets.items():
            for target in targets:
                if target in nodes:
                    reverse_routes[target].add(source)
        can_reach_ending = set(terminal_nodes)
        stack = list(terminal_nodes)
        while stack:
            node_id = stack.pop()
            for source in reverse_routes.get(node_id, set()):
                if source not in can_reach_ending:
                    can_reach_ending.add(source)
                    stack.append(source)
        for node_id in sorted(reachable.difference(can_reach_ending)):
            if node_id not in terminal_nodes:
                c.add("node_cannot_reach_ending", f"nodes.{node_id}", "该幕所在路线无法最终到达结局节点。")

    @staticmethod
    def _warnings(story: Mapping[str, Any]) -> list[NumericV2Warning]:
        used: set[str] = set()
        for node in story.get("nodes", []):
            for route in node.get("route_gates", []):
                conditions = route.get("conditions", {})
                rows = conditions.get("all") or conditions.get("any") or []
                used.update(row.get("metric") for row in rows if isinstance(row, Mapping))
        return [
            NumericV2Warning(
                "unused_metric",
                f"metric_schema.{metric_id}",
                "该 metric 尚未被任何路线使用。",
            )
            for metric_id in story.get("metric_schema", {})
            if metric_id not in used
        ]


__all__ = [
    "CompiledNumericV2Package",
    "NumericV2CompileError",
    "NumericV2Compiler",
    "NumericV2Issue",
    "NumericV2Warning",
    "MAX_RECOMMENDED_TURNS",
    "STORY_SCHEMA",
]
