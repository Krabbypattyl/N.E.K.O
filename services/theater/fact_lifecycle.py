"""从运行时账本派生并校验 Node 的事实生命周期投影。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


FACT_LIFECYCLE_SCHEMA_VERSION = "fact_lifecycle_v1"
FACT_LIFECYCLE_MIGRATION_STATUS_FIELD = "fact_lifecycle_migration_status"
# 三态字段是跨仓边界的诊断协议；只有 ``complete`` 可以真正进入执行链路。
FACT_LIFECYCLE_MIGRATION_STATUSES = frozenset({
    "legacy_readonly",
    "migration_required",
    "complete",
})
MigrationStatus = Literal["legacy_readonly", "migration_required", "complete"]
MigrationBoundary = Literal["compile", "import", "runtime"]
_EVENT_FIELDS = (
    "entry_event_ids",
    "opening_delivery_event_ids",
    "pending_player_commit_event_ids",
    "must_not_reask_event_ids",
)
_MAX_EVENT_IDS = 8


@dataclass(frozen=True)
class FactLifecycleIssue:
    """一条可稳定断言的投影错误。"""  # noqa: DOCSTRING_CJK

    code: str
    path: str
    message: str


def migration_status_issues(
    story: dict[str, Any],
    *,
    path: str = FACT_LIFECYCLE_MIGRATION_STATUS_FIELD,
    boundary: MigrationBoundary = "import",
) -> list[FactLifecycleIssue]:
    """在指定边界执行生命周期闸门，不为旧包提供兼容执行路径。"""  # noqa: DOCSTRING_CJK

    nodes = story.get("narrative_nodes")
    node_list = nodes if isinstance(nodes, list) else []
    raw_status = story.get(FACT_LIFECYCLE_MIGRATION_STATUS_FIELD)
    if boundary not in {"compile", "import", "runtime"}:
        raise ValueError(f"unsupported_fact_lifecycle_boundary:{boundary}")
    if raw_status is not None and (
        not isinstance(raw_status, str)
        or raw_status not in FACT_LIFECYCLE_MIGRATION_STATUSES
    ):
        return [FactLifecycleIssue(
            "fact_lifecycle_migration_status_invalid",
            path,
            "fact_lifecycle_migration_status 只能是 legacy_readonly、migration_required 或 complete。",
        )]
    status = classify_migration_status(story)
    if status == "legacy_readonly":
        code = {
            "compile": "fact_lifecycle_legacy_requires_regeneration",
            "import": "fact_lifecycle_legacy_import_blocked",
            "runtime": "fact_lifecycle_legacy_runtime_blocked",
        }[boundary]
        return [FactLifecycleIssue(
            code,
            path,
            "旧 Story 仅可被识别为 legacy_readonly，必须重新生成完整 Story 后才能继续。",
        )]
    if status == "migration_required":
        return [FactLifecycleIssue(
            "fact_lifecycle_migration_required",
            path,
            f"Story 处于迁移中，{boundary} 边界禁止自动迁移或继续执行。",
        )]
    if any(
        not isinstance(node, dict) or "entry_protocol" not in node
        for node in node_list
    ):
        return [FactLifecycleIssue(
            "fact_lifecycle_projection_incomplete",
            "narrative_nodes",
            "complete Story 的每个 Node 都必须包含编译器派生的 EntryProtocol。",
        )]
    return []


def classify_migration_status(story: dict[str, Any]) -> MigrationStatus:
    """根据显式标记和投影形态识别三态；不替 Story 补写状态。"""  # noqa: DOCSTRING_CJK

    raw_status = story.get(FACT_LIFECYCLE_MIGRATION_STATUS_FIELD)
    if isinstance(raw_status, str) and raw_status in FACT_LIFECYCLE_MIGRATION_STATUSES:
        return raw_status  # type: ignore[return-value]
    nodes = story.get("narrative_nodes")
    node_list = nodes if isinstance(nodes, list) else []
    has_projection = bool(node_list) and all(
        isinstance(node, dict) and "entry_protocol" in node for node in node_list
    )
    # 缺字段且没有新投影表示旧包；已有部分新投影但没有 marker 表示迁移未完成。
    return "migration_required" if has_projection else "legacy_readonly"


def derive_entry_protocol(
    node: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """只从 Event、Node 和当前 Node 的 Choice 出边派生投影。"""  # noqa: DOCSTRING_CJK

    node_id = str(node.get("node_id") or "")
    entry_ids = _ids(node.get("entry_event_ids"))
    acting_beats = node.get("acting_beats")
    opening_ids = _ids(
        acting_beats.get("must_publish_event_ids")
        if isinstance(acting_beats, dict)
        else []
    )
    pending_ids: list[str] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from_node", edge.get("source"))
        if str(source or "") != node_id:
            continue
        trigger = edge.get("trigger")
        if not isinstance(trigger, dict) or trigger.get("kind") != "choice":
            continue
        commit_event_id = str(trigger.get("commit_event_id") or "")
        if commit_event_id and commit_event_id not in pending_ids:
            pending_ids.append(commit_event_id)

    event_index = {
        str(event.get("event_id") or ""): event
        for event in events
        if isinstance(event, dict) and str(event.get("event_id") or "")
    }
    must_not_reask = [
        event_id
        for event_id in entry_ids
        if event_index.get(event_id, {}).get("modality", "actual") == "actual"
    ]
    return {
        "schema_version": FACT_LIFECYCLE_SCHEMA_VERSION,
        "entry_event_ids": entry_ids,
        "opening_delivery_event_ids": opening_ids,
        "pending_player_commit_event_ids": pending_ids,
        "must_not_reask_event_ids": must_not_reask,
    }


def validate_entry_protocol(
    protocol: dict[str, Any] | None,
    *,
    node: dict[str, Any],
    events: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    path: str,
) -> list[FactLifecycleIssue]:
    """校验编译器派生的投影，不接受它作为事实真源。"""  # noqa: DOCSTRING_CJK

    if protocol is None:
        return []
    issues: list[FactLifecycleIssue] = []
    if not isinstance(protocol, dict) or protocol.get("schema_version") != FACT_LIFECYCLE_SCHEMA_VERSION:
        return [FactLifecycleIssue(
            "fact_lifecycle_missing",
            f"{path}.schema_version",
            "EntryProtocol 必须使用 fact_lifecycle_v1。",
        )]
    allowed_fields = {"schema_version", *_EVENT_FIELDS}
    if set(protocol) != allowed_fields:
        issues.append(FactLifecycleIssue(
            "fact_lifecycle_missing",
            path,
            "EntryProtocol 字段必须与 fact_lifecycle_v1 完全一致。",
        ))
    actual: dict[str, list[str]] = {}
    for field in _EVENT_FIELDS:
        raw_values = protocol.get(field)
        if not isinstance(raw_values, list) or len(raw_values) > _MAX_EVENT_IDS:
            issues.append(FactLifecycleIssue(
                "fact_lifecycle_missing",
                f"{path}.{field}",
                "EntryProtocol 事件字段必须是最多 8 项的 ID 数组。",
            ))
            actual[field] = []
            continue
        if any(not _valid_id(value) for value in raw_values):
            issues.append(FactLifecycleIssue(
                "fact_lifecycle_missing",
                f"{path}.{field}",
                "EntryProtocol 只能引用非空、未填充的稳定 Event ID。",
            ))
        if len(raw_values) != len(set(raw_values)):
            issues.append(FactLifecycleIssue(
                "fact_lifecycle_missing",
                f"{path}.{field}",
                "同一 EntryProtocol 事件集合内不能重复引用 Event。",
            ))
        actual[field] = [str(item) for item in raw_values]

    event_index = {
        str(event.get("event_id") or ""): event
        for event in events
        if isinstance(event, dict) and str(event.get("event_id") or "")
    }
    for field, ids in actual.items():
        for index, event_id in enumerate(ids):
            if event_id not in event_index:
                issues.append(FactLifecycleIssue(
                    "fact_lifecycle_unknown_event",
                    f"{path}.{field}[{index}]",
                    "EntryProtocol 引用了不存在的 Event。",
                ))

    expected = derive_entry_protocol(node, events=events, edges=edges)
    for field in _EVENT_FIELDS:
        if actual.get(field) != expected[field]:
            issues.append(FactLifecycleIssue(
                "fact_lifecycle_projection_mismatch",
                f"{path}.{field}",
                "EntryProtocol 必须由当前 Node、Event 目录和 Choice 出边自动派生。",
            ))
    if set(actual["opening_delivery_event_ids"]) & set(actual["entry_event_ids"]):
        issues.append(FactLifecycleIssue(
            "fact_lifecycle_stage_conflict",
            f"{path}.opening_delivery_event_ids",
            "已在 Node 入口成立的 Event 不能再次作为 opening delivery。",
        ))
    return _deduplicate(issues)


def _ids(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        item_id = str(item or "")
        if item_id and item_id not in result:
            result.append(item_id)
    return result


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _deduplicate(issues: list[FactLifecycleIssue]) -> list[FactLifecycleIssue]:
    seen: set[tuple[str, str]] = set()
    result: list[FactLifecycleIssue] = []
    for issue in issues:
        key = (issue.code, issue.path)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
