"""校验小剧场 Story 根结构和静态作者图。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from typing import Any
import unicodedata

from . import fact_lifecycle
from .authoring_dto import TARGET_STATE_EVIDENCE_FIELD
from .time_anchor_contract import numeric_time_anchor_issues


# 表演节拍会进入 Actor Prompt；合同级上限既防止作者界面误存巨量文本，
# 也保证所有新字段都处于现有小剧场输入预算之内。
_BEAT_TEXT_MAX_CHARS = 320
_BEAT_ITEM_MAX_CHARS = 160
_BEAT_LIST_MAX_ITEMS = 8
_REQUIRED_TERM_MAX_CHARS = 48
_PACING_TEXT_MAX_CHARS = 160
# SceneSpec v1 只声明一个 Scene 在整个阶段内不随回合改变的舞台事实。
# 时间属于 continuity Ledger，当前目标属于 Node，合法出口属于 Edge；把它们
# 复制进 SceneSpec 会重新制造多份可写世界状态。
_SCENE_SPEC_SCHEMA_VERSION = "scene_spec_v1"
# 跳场守卫把地点作为精确、可见的命名锚点匹配；SceneSpec 不能把整段环境
# 描述伪装成地点，否则 Prompt 与守卫会采用不同的可识别边界。
_SCENE_SPEC_LOCATION_MAX_CHARS = 48
_SCENE_SPEC_VISIBLE_PROP_MAX_ITEMS = 8
_SCENE_ENVIRONMENT_DETAIL_MAX_ITEMS = 8
_SCENE_ENVIRONMENT_DETAIL_MAX_CHARS = 48
_SCENE_SPEC_FIELDS = frozenset(
    {"location_name", "present_roles", "visible_prop_ids"}
)
_SCENE_SPEC_ROLE_IDS = ("player", "active_catgirl")
_SCENE_SPEC_LEGACY_SCENE_FIELDS = frozenset(
    {"location_name", "location", "time_context", "time", "current_goal"}
)
# 连续性目录每轮都会进入 Router 与 Actor；合同上限与文档一致，防止
# 作者端误存的长文本绕过生成预算并在每个回合重复放大。
_CONTINUITY_LABEL_MAX_CHARS = 48
_CONTINUITY_SUMMARY_MAX_CHARS = 160
_CONTINUITY_BOUNDARY_MAX_CHARS = 160
_CONTINUITY_BOUNDARY_MAX_ITEMS = 8
_CONTINUITY_SCHEMA_VERSION = "continuity_v1"
# 运行时账本是 Story v3 的唯一权威状态来源，不能把自然语言事实猜成事件证据。
_RUNTIME_LEDGER_SCHEMA_VERSION = "theater_ledger_v2"
_LEDGER_EVENT_TEXT_MAX_CHARS = 160
_LEDGER_EVENT_TYPES = frozenset(
    {
        "speech",
        "action",
        "observation",
        "environment_change",
        "movement",
        "reveal",
        "acquire",
        "consume",
        "relationship_evidence",
        "acknowledgement",
    }
)
_LEDGER_EVENT_ACTORS = frozenset({"player", "catgirl", "active_catgirl", "environment"})
_LEDGER_EVENT_VISIBILITIES = frozenset({"public", "world_only"})
_LEDGER_EVENT_AGENCIES = frozenset(
    {"author_seed", "player_commit", "npc", "environment"}
)
_LEDGER_EVENT_MODALITIES = frozenset(
    {"actual", "attempted", "hypothetical", "questioned"}
)
_LEDGER_EVENT_CHANNELS = frozenset(
    {"player_input", "dialogue", "narration", "transition", "private"}
)
_LEDGER_EVENT_EVIDENCE_MAX_ITEMS = 8
_LEDGER_EVENT_EVIDENCE_MAX_CHARS = 48
_STATE_EFFECT_KINDS = frozenset(
    {"fact", "time", "location", "relationship", "prop", "clue", "open_loop"}
)
_STATE_EFFECT_OPS = frozenset(
    {
        "set",
        "assert",
        "retract",
        "acquire",
        "consume",
        "reveal",
        "resolve",
        "supersede",
        "expire",
    }
)
_STATE_EFFECT_FIELDS = frozenset(
    {
        "effect_id",
        "state_kind",
        "state_id",
        "op",
        "from_value",
        "value",
        "evidence_event_id",
    }
)
# Story v3 是当前唯一可导入的运行时合同：Choice 归 Edge 所有，事件账本由
# 服务端维护，Actor 只提交已交付事件 ID，不再存在旧包双读路径。
STORY_SCHEMA_VERSION = "neko_theater_story_v3"
_EDGE_CHOICE_SUPPORTED_TRIGGER_KINDS = frozenset({"choice", "automatic"})
_EDGE_CHOICE_LEGACY_EDGE_ROUTING_FIELDS = frozenset(
    {
        "choice_id",
        "visibility",
        "transition_id",
        "intent_id",
        "intent_summary",
        "intent_examples",
        "callback",
    }
)
_STORY_V3_REMOVED_ROOT_FIELDS = frozenset({"opening_dialogue", "ending_attractors"})

# 现代 Scene 只建立环境基线，猫娘现场对白由 acting_beats 驱动 Actor 生成。
# 以下规则只识别带有说话人或明确言说动作的高置信对白；不能因为正文里
# 出现引号，就把门牌、记录或书名误判为角色发言。
_SCENE_PARTICIPANT_RE = r"(?:当前猫娘|猫娘|她|你|\{\{lanlan_name\}\})"
_SCENE_SPEAKER_ACTION_RE = (
    r"(?:微笑|笑|点头|摇头|抬头|低头|转身|歪头|侧身|停顿|犹豫|"
    r"回头|看向|望向|对你|向你|清嗓|顿了顿|开口|轻声|低声|小声|"
    r"温柔|认真|好奇|直接|主动|忽然|突然|随后|接着)"
)
# 言说动词必须和说话人处于同一个、无逗号的短动作片段。这个约束能
# 区分“她微笑着说”与“你面前的屏幕提示说”，避免跨对象寻找动词。
_SCENE_SPEAKER_LEAD_IN_RE = (
    r"(?:\s*|[^，,。！？；\n]{0,16}"
    + _SCENE_SPEAKER_ACTION_RE
    + r"[^，,。！？；\n]{0,8})"
)
_SCENE_SPEECH_CUE_RE = (
    r"(?:说道|问道|喊道|答道|叫道|笑道|补充道|解释道|低语|呢喃|"
    r"开口(?:说|问|道)?|(?<!的)(?:回答|回应|告诉|提醒|表示|询问|提问|追问)|"
    # “说明 / 说服”“问卷 / 问题”等是环境叙述中的常见词，不能按裸动词截断。
    r"(?<![小传游解听重学演诉胡话叙评假据])说(?!明|服|书|法|辞|教|理)|"
    r"(?<![顾疑提学反慰审盘访询追])问(?!卷|题|答|候|路|诊|询)|"
    r"(?<![呐呼叫])喊(?!价|话|声))"
)
# 带引号时只需要确认明确言说结构，不需要猜前面的词是不是姓名。
# 这能覆盖任意题材和命名方式，同时让“终端显示‘访问被拒绝’”保持合法。
_SCENE_QUOTED_SPEECH_CUE_RE = (
    r"(?:说道|问道|喊道|答道|叫道|笑道|补充道|解释道|低语|呢喃|"
    r"开口(?:说|问|道)?|回答|回应|告诉|"
    r"(?<![小传游解听重学演诉胡话叙评假据])说(?!明|服|书|法|辞|教|理)|"
    r"(?<![顾疑提学反慰审盘访询追])问(?!卷|题|答|候|路|诊|询)|"
    r"(?<![呐呼叫])喊(?!价|话|声))"
)
_SCENE_OPEN_QUOTE_RE = r"[“‘\"'「『]"
_SCENE_CLOSE_QUOTE_RE = r"[”’\"'」』]"
# 书名内部可能天然出现“她问”“你说”等字样；先遮罩完整书名，再判断
# Scene 中剩余文本，避免把作品标题当成当前角色的现场发言。
_SCENE_BOOK_TITLE_RE = re.compile(r"《[^》\n]{1,80}》")
_SCENE_LIVE_SPEECH_BEFORE_QUOTE_RE = re.compile(
    _SCENE_QUOTED_SPEECH_CUE_RE
    + r"\s*[，,:：]?\s*"
    + _SCENE_OPEN_QUOTE_RE
)
_SCENE_LIVE_SPEECH_WITHOUT_QUOTE_RE = re.compile(
    _SCENE_PARTICIPANT_RE
    + _SCENE_SPEAKER_LEAD_IN_RE
    + _SCENE_SPEECH_CUE_RE
)
# “她缓缓地说道 / 她试探性地问你”没有有限动作词，但仍是高置信
# 转述。缓冲区禁止“的”，避免跨进“她面前的手机提醒你”等环境主体。
_SCENE_PARTICIPANT_ADVERBIAL_SPEECH_RE = re.compile(
    _SCENE_PARTICIPANT_RE
    + r"[^的，,。！？；\n]{0,16}"
    + r"(?:说道|问道|喊道|答道|叫道|笑道|补充道|解释道|低语|呢喃|"
    + r"问(?=你|我|他|她|大家|是否|要不要|能不能|可不可以))"
)
_SCENE_LIVE_SPEECH_AFTER_QUOTE_RE = re.compile(
    _SCENE_CLOSE_QUOTE_RE
    + r"[^。！？\n]{0,24}(?:"
    + _SCENE_QUOTED_SPEECH_CUE_RE
    + r"|(?:的)?(?:声音|语气|嗓音)[^。！？\n]{0,24})"
)
_SCENE_PARTICIPANT_ACTION_COLON_RE = re.compile(
    _SCENE_PARTICIPANT_RE + r"\s*[：:]\s*" + _SCENE_OPEN_QUOTE_RE
)
_SCENE_ACTION_COLON_RE = re.compile(
    _SCENE_SPEAKER_ACTION_RE
    + r"[^。！？；\n]{0,8}"
    + r"[：:]\s*"
    + _SCENE_OPEN_QUOTE_RE
)
_SCENE_VOICE_COLON_RE = re.compile(
    r"(?:声音|语气|嗓音)[^。！？；\n]{0,20}[：:]\s*"
    + _SCENE_OPEN_QUOTE_RE
)


class StoryRootNotObjectError(ValueError):
    """Story Package 顶层不是 JSON object。"""  # noqa: DOCSTRING_CJK


def initial_node_id(story: dict[str, Any]) -> str:
    """取得 Loader 已验证的唯一 setup seed 节点。"""  # noqa: DOCSTRING_CJK
    nodes = [
        node for node in story.get("narrative_nodes") or [] if isinstance(node, dict)
    ]
    for node in nodes:
        if node.get("node_type") == "seed" and node.get("belong_phase") == "setup":
            return str(node.get("node_id") or "")
    return ""


def _validate_edge_choice_transition_boundary(
    story: dict[str, Any], path: Path
) -> None:
    """拒绝 Story v3 中已经删除的旧字段，避免协议出现第二份真源。"""  # noqa: DOCSTRING_CJK
    if _STORY_V3_REMOVED_ROOT_FIELDS.intersection(story):
        # 开场固定对白和结束吸引器都曾绕过当前 Node/Actor 合同；v3
        # 不再读取它们，连导入也直接拒绝，避免残留字段产生误导。
        raise ValueError(
            f"Theater story {path} Story v3 contains removed root fields"
        )
    for node in story.get("narrative_nodes") or []:
        if not isinstance(node, dict):
            continue
        if "scripted_dialogue" in node or "suggestions" not in node:
            raise ValueError(
                f"Theater story {path} Story v3 node contains removed legacy fields"
            )
        if node.get("suggestions"):
            raise ValueError(
                f"Theater story {path} Story v3 node contains target-owned suggestions"
            )
    for edge in story.get("edges") or []:
        if isinstance(edge, dict) and _EDGE_CHOICE_LEGACY_EDGE_ROUTING_FIELDS.intersection(edge):
            raise ValueError(
                f"Theater story {path} Story v3 edge contains removed legacy routing fields"
            )


def validate_story_package(story: dict[str, Any], path: Path) -> dict[str, Any]:
    """执行唯一 Story v3 协议检查，阻止旧包和断边图进入运行时。"""  # noqa: DOCSTRING_CJK
    schema_version = story.get("story_schema_version")
    if schema_version != STORY_SCHEMA_VERSION:
        # 旧版字段和缺省 marker 都不再自动迁移；作者端必须重新编译成 v3。
        raise ValueError(f"Theater story {path} has unsupported story schema version")
    if story.get("runtime_ledger_schema_version") != _RUNTIME_LEDGER_SCHEMA_VERSION:
        # Story v3 的入口前提和事件交付依赖同一份运行时账本，不能降级到无账本模式。
        raise ValueError(f"Theater story {path} is missing Story v3 runtime ledger")
    # Import 只接收完整编译产物；旧包和迁移中包均不进入读取链路。
    migration_issues = fact_lifecycle.migration_status_issues(
        story,
        boundary="import",
    )
    if migration_issues:
        issue = migration_issues[0]
        raise ValueError(
            f"Theater story {path} {issue.code} at {issue.path}: {issue.message}"
        )
    time_anchor_issues = numeric_time_anchor_issues(story, "story")
    if time_anchor_issues:
        issue = time_anchor_issues[0]
        raise ValueError(
            f"Theater story {path} {issue['code']} at {issue['path']}: {issue['message']}"
        )
    _validate_edge_choice_transition_boundary(story, path)
    required = (
        "id",
        "title",
        "background",
        "initial_scene_id",
        "scenes",
        "narrative_nodes",
        "edges",
    )
    missing = [key for key in required if not story.get(key)]
    if missing:
        raise ValueError(f"Theater story {path} missing fields: {', '.join(missing)}")
    raw_scenes = story.get("scenes")
    if not isinstance(raw_scenes, list) or any(
        not isinstance(scene, dict) for scene in raw_scenes
    ):
        raise ValueError(f"Theater story {path} has invalid scenes")
    scenes = list(raw_scenes)
    scene_ids = [str(scene.get("id") or "") for scene in scenes]
    if (
        not scene_ids
        or any(not scene_id for scene_id in scene_ids)
        or len(scene_ids) != len(set(scene_ids))
    ):
        raise ValueError(f"Theater story {path} has invalid or duplicate scene ids")
    if str(story.get("initial_scene_id") or "") not in scene_ids:
        raise ValueError(f"Theater story {path} initial scene references unknown scene")
    scene_phases: set[str] = set()
    scene_phase_by_id: dict[str, str] = {}
    for scene in scenes:
        phase = str(scene.get("phase") or "").strip()
        if (
            not phase
            or not str(scene.get("title") or "").strip()
            or not str(scene.get("text") or "").strip()
        ):
            raise ValueError(f"Theater story {path} has incomplete scene")
        if phase in scene_phases:
            raise ValueError(f"Theater story {path} has duplicate scene phase: {phase}")
        scene_phases.add(phase)
        scene_phase_by_id[str(scene.get("id") or "")] = phase
        environment_details = scene.get("environment_details", [])
        normalized_environment_details = [
            detail.strip() if isinstance(detail, str) else detail
            for detail in environment_details
        ] if isinstance(environment_details, list) else []
        if (
            not isinstance(environment_details, list)
            or len(environment_details) > _SCENE_ENVIRONMENT_DETAIL_MAX_ITEMS
            or any(
                not isinstance(detail, str)
                or not detail.strip()
                or len(detail.strip()) > _SCENE_ENVIRONMENT_DETAIL_MAX_CHARS
                for detail in environment_details
            )
            or len(normalized_environment_details) != len(set(normalized_environment_details))
        ):
            raise ValueError(f"Theater story {path} has invalid environment details")
    if scene_phase_by_id.get(str(story.get("initial_scene_id") or "")) != "setup":
        # 开演预览和 Session seed 必须落在同一张 setup 舞台；否则预览图与
        # Rules 按 seed 节点初始化的 SceneState 会从第一帧开始分叉。
        raise ValueError(f"Theater story {path} initial scene must use setup phase")
    raw_nodes = story.get("narrative_nodes")
    if not isinstance(raw_nodes, list) or any(
        not isinstance(node, dict) for node in raw_nodes
    ):
        raise ValueError(f"Theater story {path} has invalid narrative nodes")
    nodes = list(raw_nodes)
    node_ids = [str(node.get("node_id") or "") for node in nodes]
    if (
        not node_ids
        or any(not node_id for node_id in node_ids)
        or len(node_ids) != len(set(node_ids))
    ):
        raise ValueError(f"Theater story {path} has invalid or duplicate node ids")
    _validate_scene_spec_contract(story, path, scenes, nodes)
    _validate_modern_scene_text_contract(scenes, nodes, path)
    _validate_public_story_contract(story, path)
    _validate_static_node_contract(story, nodes, path, scene_phases)
    raw_edges = story.get("edges")
    if not isinstance(raw_edges, list):
        raise ValueError(f"Theater story {path} has invalid edges")
    edges = list(raw_edges)
    edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError(f"Theater story {path} has invalid edge")
        if (
            str(edge.get("from_node") or "") not in node_ids
            or str(edge.get("to_node") or "") not in node_ids
        ):
            raise ValueError(f"Theater story {path} edge references unknown node")
        if "edge_id" in edge:
            raw_edge_id = edge.get("edge_id")
            edge_id = raw_edge_id.strip() if isinstance(raw_edge_id, str) else ""
            if not edge_id:
                raise ValueError(f"Theater story {path} has invalid edge id")
            if edge_id in edge_ids:
                raise ValueError(
                    f"Theater story {path} has duplicate edge id: {edge_id}"
                )
            edge_ids.add(edge_id)
        edge_transition_beat = edge.get("transition_beat")
        if edge_transition_beat is not None and not _valid_transition_beat(
            edge_transition_beat
        ):
            raise ValueError(
                f"Theater story {path} edge has invalid transition beat"
            )
        # Story v3 的 Edge trigger 和 Choice 形状由统一图合同一次性校验；
        # 这里不再保留 visibility、callback 或 target suggestions 分支。
        continue
    _validate_scene_prop_references(story, path, scenes, nodes, edges)
    if not initial_node_id(story):
        raise ValueError(f"Theater story {path} has no setup node")
    _validate_pacing_graph_contract(nodes, edges, path)
    _validate_static_graph_contract(story, path, nodes, edges)
    _validate_continuity_contract(story, path, nodes, edges)
    _validate_runtime_ledger_contract(story, path, nodes, edges)
    _validate_reachable_ending(story, path)
    return deepcopy(story)


def _validate_scene_spec_contract(
    story: dict[str, Any],
    path: Path,
    scenes: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> None:
    """校验必需 SceneSpec v1，拒绝场景事实在 Node 或旧字段中重复声明。"""  # noqa: DOCSTRING_CJK

    has_schema_version = "scene_spec_schema_version" in story
    scene_spec_count = sum("scene_spec" in scene for scene in scenes)
    has_node_scene_spec = any("scene_spec" in node for node in nodes)
    if not has_schema_version:
        raise ValueError(f"Theater story {path} has invalid scene spec schema version")
    if story.get("scene_spec_schema_version") != _SCENE_SPEC_SCHEMA_VERSION:
        raise ValueError(f"Theater story {path} has invalid scene spec schema version")
    if has_node_scene_spec:
        # Node 随图推进，SceneSpec 只表达稳定舞台事实。二者同时拥有地点或
        # 道具会让自由交流在未过 Edge 时出现隐性换景。
        raise ValueError(f"Theater story {path} contains node-owned scene spec")
    if scene_spec_count != len(scenes):
        raise ValueError(f"Theater story {path} has incomplete scene spec")

    stage_prop_ids = {
        str(prop.get("id") or "").strip()
        for prop in story.get("stage_props") or []
        if isinstance(prop, dict) and str(prop.get("id") or "").strip()
    }
    for scene in scenes:
        if _SCENE_SPEC_LEGACY_SCENE_FIELDS.intersection(scene):
            # 同一 Scene 不能既写 v1 规格又保留旧 location/time/goal 旁路；
            # 后者必须分别由 SceneSpec、Ledger 和 Node 继续唯一拥有。
            raise ValueError(f"Theater story {path} scene spec duplicates legacy fields")
        spec = scene.get("scene_spec")
        if not isinstance(spec, dict) or set(spec) != _SCENE_SPEC_FIELDS:
            raise ValueError(f"Theater story {path} has invalid scene spec")
        location_name = spec.get("location_name")
        if (
            not isinstance(location_name, str)
            or not location_name.strip()
            or len(location_name.strip()) > _SCENE_SPEC_LOCATION_MAX_CHARS
        ):
            raise ValueError(f"Theater story {path} has invalid scene spec location")
        present_roles = spec.get("present_roles")
        if present_roles != list(_SCENE_SPEC_ROLE_IDS):
            # v1 运行时只有玩家与当前猫娘两个公开 Actor。允许半份或未知角色
            # 会让 Actor 仍生成猫娘对白、但场景锚点却声称她不在场。
            raise ValueError(f"Theater story {path} has invalid scene spec roles")
        visible_prop_ids = spec.get("visible_prop_ids")
        if (
            not isinstance(visible_prop_ids, list)
            or len(visible_prop_ids) > _SCENE_SPEC_VISIBLE_PROP_MAX_ITEMS
            or any(
                not isinstance(prop_id, str)
                or not prop_id
                or prop_id not in stage_prop_ids
                for prop_id in visible_prop_ids
            )
            or len(visible_prop_ids) != len(set(visible_prop_ids))
        ):
            raise ValueError(f"Theater story {path} has invalid scene spec props")


def _validate_modern_scene_text_contract(
    scenes: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    path: Path,
) -> None:
    """Reject live dialogue only where a modern Actor contract owns that phase."""  # noqa: DOCSTRING_CJK

    modern_phases = {
        str(node.get("belong_phase") or "").strip()
        for node in nodes
        if node.get("acting_beats") is not None
    }
    patterns = (
        _SCENE_LIVE_SPEECH_BEFORE_QUOTE_RE,
        _SCENE_LIVE_SPEECH_WITHOUT_QUOTE_RE,
        _SCENE_PARTICIPANT_ADVERBIAL_SPEECH_RE,
        _SCENE_LIVE_SPEECH_AFTER_QUOTE_RE,
        _SCENE_PARTICIPANT_ACTION_COLON_RE,
        _SCENE_ACTION_COLON_RE,
        _SCENE_VOICE_COLON_RE,
    )
    for scene in scenes:
        if str(scene.get("phase") or "").strip() not in modern_phases:
            continue
        text = _SCENE_BOOK_TITLE_RE.sub("", str(scene.get("text") or ""))
        if any(pattern.search(text) for pattern in patterns):
            # 错误只暴露稳定 Scene ID，不把作者正文重复写入日志。
            scene_id = str(scene.get("id") or "")
            raise ValueError(
                f"Theater story {path} scene contains live character dialogue: "
                f"{scene_id}"
            )


def _validate_scene_prop_references(
    story: dict[str, Any],
    path: Path,
    scenes: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """在 Compile/Import 边界阻断未声明、不可见或无证据的舞台道具。

    SceneSpec 是当前舞台唯一可见性来源。道具引用必须通过显式 ``prop_refs``
    或已登记目录 label 解析为稳定 ID；不对自然语言正文做词汇猜测，避免把
    某个题材的名词黑名单误当成跨剧本合同。
    """

    raw_catalog = story.get("stage_props") or []
    if not isinstance(raw_catalog, list):
        raise ValueError(f"Theater story {path} has invalid prop catalog")
    props: dict[str, dict[str, Any]] = {}
    labels: dict[str, str] = {}
    for prop in raw_catalog:
        if not isinstance(prop, dict):
            raise ValueError(f"Theater story {path} has invalid prop catalog")
        prop_id = str(prop.get("id") or "").strip()
        label = str(prop.get("label") or "").strip()
        if prop_id:
            props[prop_id] = prop
        if label:
            labels[label] = prop_id

    visible_by_phase = {
        str(scene.get("phase") or ""): set(
            scene.get("scene_spec", {}).get("visible_prop_ids") or []
        )
        for scene in scenes
    }
    environment_labels_by_phase = {
        str(scene.get("phase") or ""): {
            detail.strip()
            for detail in scene.get("environment_details") or []
            if isinstance(detail, str) and detail.strip()
        }
        for scene in scenes
        if isinstance(scene, dict)
    }
    node_by_id = {str(node.get("node_id") or ""): node for node in nodes}

    def reject_environment_detail_action(
        text: Any, *, owner_name: str, phase: str
    ) -> None:
        """Keep descriptive Scene details out of executable author actions."""
        source = str(text or "")
        # 不仅阻止操作当前 Scene 的背景细节，也阻止 Choice 提前引用目标
        # Scene 才会成立的背景细节，避免把未来舞台物件泄漏到来源动作。
        all_environment_labels = {
            label
            for labels_for_phase in environment_labels_by_phase.values()
            for label in labels_for_phase
        }
        if any(label and label in source for label in all_environment_labels):
            raise ValueError(
                f"Theater story {path} environment_detail_used_as_action at {owner_name}"
            )

    def state_effect_ids(owner: dict[str, Any]) -> set[str]:
        effects = owner.get("node_state_effects", [])
        if "state_effects" in owner:
            effects = owner.get("state_effects", [])
        return {
            str(effect.get("state_id") or "")
            for effect in effects or []
            if isinstance(effect, dict) and effect.get("state_kind") == "prop"
        }

    def inspect(
        text: Any,
        *,
        owner_name: str,
        phase: str,
        owner: dict[str, Any],
        requires_effect: bool,
        explicit_refs: Any = None,
    ) -> None:
        explicit = [] if explicit_refs is None else explicit_refs
        if not isinstance(explicit, list) or any(
            not _valid_continuity_id(item) for item in explicit
        ) or len(explicit) != len(set(explicit)):
            raise ValueError(f"Theater story {path} has invalid prop_refs: {owner_name}")
        referenced: set[str] = set(explicit)
        source = str(text or "")
        # Structured IDs are authoritative; labels are accepted only as a
        # temporary compiler bridge and still have to pass visibility/evidence.
        referenced.update(
            prop_id
            for label, prop_id in labels.items()
            if prop_id and label in source
        )
        for prop_id in referenced:
            if prop_id not in props:
                raise ValueError(
                    f"Theater story {path} undeclared_scene_prop at {owner_name}"
                )
            if prop_id not in visible_by_phase.get(phase, set()):
                raise ValueError(
                    f"Theater story {path} prop_not_visible_in_scene at {owner_name}"
                )
            if requires_effect and prop_id not in state_effect_ids(owner):
                raise ValueError(
                    f"Theater story {path} prop_effect_missing at {owner_name}"
                )

    for scene in scenes:
        phase = str(scene.get("phase") or "")
        inspect(
            f"{scene.get('title', '')} {scene.get('text', '')}",
            owner_name=f"scene:{scene.get('id')}",
            phase=phase,
            owner=scene,
            requires_effect=False,
        )
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        phase = str(node.get("belong_phase") or "")
        beats = node.get("acting_beats") if isinstance(node.get("acting_beats"), dict) else {}
        refs = beats.get("prop_refs")
        inspect(
            " ".join(
                [
                    str(beats.get("narrator_goal") or ""),
                    str(beats.get("catgirl_goal") or ""),
                    " ".join(str(item) for item in beats.get("required_terms", []) or []),
                ]
            ),
            owner_name=f"node:{node_id}.acting_beats",
            phase=phase,
            owner=node,
            requires_effect=False,
            explicit_refs=refs,
        )
        inspect(
            " ".join(str(item) for item in beats.get("must_deliver", []) or []),
            owner_name=f"node:{node_id}.acting_beats.must_deliver",
            phase=phase,
            owner=node,
            requires_effect=True,
        )
        inspect(
            " ".join(str(item) for item in beats.get("action_cues", []) or []),
            owner_name=f"node:{node_id}.acting_beats.action_cues",
            phase=phase,
            owner=node,
            requires_effect=True,
        )
        action = node.get("script_action")
        if isinstance(action, dict):
            inspect(
                "",
                owner_name=f"node:{node_id}.script_action.uses_props",
                phase=phase,
                owner=node,
                requires_effect=True,
                explicit_refs=action.get("uses_props", []),
            )
        for field in ("action", "action_text"):
            if isinstance(node.get(field), str):
                reject_environment_detail_action(
                    node[field], owner_name=f"node:{node_id}.{field}", phase=phase
                )
                inspect(
                    node[field],
                    owner_name=f"node:{node_id}.{field}",
                    phase=phase,
                    owner=node,
                    requires_effect=True,
                )
    for edge in edges:
        source_id = str(edge.get("from_node") or "")
        phase = str(node_by_id.get(source_id, {}).get("belong_phase") or "")
        beat = edge.get("transition_beat") if isinstance(edge.get("transition_beat"), dict) else {}
        inspect(
            " ".join(
                [
                    str(beat.get("player_action") or ""),
                    str(beat.get("observable_result") or ""),
                    " ".join(str(item) for item in beat.get("must_not_repeat", []) or []),
                ]
            ),
            owner_name=f"edge:{edge.get('edge_id')}.transition_beat",
            phase=phase,
            owner=edge,
            requires_effect=True,
            explicit_refs=beat.get("prop_refs"),
        )
        reject_environment_detail_action(
            beat.get("player_action"),
            owner_name=f"edge:{edge.get('edge_id')}.transition_beat.player_action",
            phase=phase,
        )
        trigger = edge.get("trigger") if isinstance(edge.get("trigger"), dict) else {}
        choice = trigger.get("choice") if isinstance(trigger.get("choice"), dict) else {}
        inspect(
            " ".join(
                [
                    str(choice.get("label") or ""),
                    " ".join(
                        str(item)
                        for item in choice.get("completion_phrases", []) or []
                    ),
                ]
            ),
            owner_name=f"edge:{edge.get('edge_id')}.trigger.choice",
            phase=phase,
            owner=edge,
            requires_effect=True,
        )
        if str(choice.get("choice_mode") or "") == "action":
            reject_environment_detail_action(
                choice.get("label"),
                owner_name=f"edge:{edge.get('edge_id')}.trigger.choice",
                phase=phase,
            )
        for field in ("action", "action_text", "spoken_text"):
            if isinstance(choice.get(field), str):
                if field != "spoken_text":
                    reject_environment_detail_action(
                        choice[field],
                        owner_name=f"edge:{edge.get('edge_id')}.trigger.choice.{field}",
                        phase=phase,
                    )
                inspect(
                    choice[field],
                    owner_name=f"edge:{edge.get('edge_id')}.trigger.choice.{field}",
                    phase=phase,
                    owner=edge,
                    requires_effect=True,
                )
        for field in ("action", "action_text"):
            if isinstance(edge.get(field), str):
                inspect(
                    edge[field],
                    owner_name=f"edge:{edge.get('edge_id')}.{field}",
                    phase=phase,
                    owner=edge,
                    requires_effect=True,
                )


def _validate_continuity_contract(
    story: dict[str, Any],
    path: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """严格校验 continuity_v1 作者合同。"""  # noqa: DOCSTRING_CJK
    has_version = "continuity_schema_version" in story
    has_catalog = "continuity_catalog" in story
    has_node_delta = any("continuity_delta" in node for node in nodes)
    if not has_version and not has_catalog and not has_node_delta:
        return
    if story.get("continuity_schema_version") != _CONTINUITY_SCHEMA_VERSION:
        # 新字段一旦出现就不能退回旧自然语言推断，否则坏包会以“兼容”为名继续演绎。
        raise ValueError(f"Theater story {path} has invalid continuity schema version")

    catalog = story.get("continuity_catalog")
    if not isinstance(catalog, dict) or set(catalog) != {
        "time_points",
        "relationship_states",
    }:
        raise ValueError(f"Theater story {path} has invalid continuity catalog")
    time_points = catalog.get("time_points")
    relationship_states = catalog.get("relationship_states")
    if not isinstance(time_points, dict) or not isinstance(
        relationship_states, dict
    ):
        raise ValueError(f"Theater story {path} has invalid continuity catalog")

    for time_id, value in time_points.items():
        if not _valid_continuity_id(time_id) or not _valid_time_point(value):
            raise ValueError(
                f"Theater story {path} has invalid continuity time point"
            )
    for relationship_id, value in relationship_states.items():
        if not _valid_continuity_id(relationship_id) or not _valid_relationship_state(
            value
        ):
            raise ValueError(
                f"Theater story {path} has invalid continuity relationship state"
            )

    time_ids = set(time_points)
    relationship_ids = set(relationship_states)
    seed_id = initial_node_id(story)
    for node in nodes:
        delta = node.get("continuity_delta")
        if delta is None and "continuity_delta" not in node:
            delta = {}
        elif not isinstance(delta, dict) or not set(delta).issubset(
            {"set_time_id", "set_relationship_state_id"}
        ):
            raise ValueError(f"Theater story {path} has invalid continuity delta")
        for field in ("set_time_id", "set_relationship_state_id"):
            if field in delta and not _valid_continuity_id(delta.get(field)):
                # 显式 null/空串不是“继承”；继承必须通过省略字段表达。
                raise ValueError(f"Theater story {path} has invalid continuity delta")
        if str(node.get("node_id") or "") == seed_id and set(delta) != {
            "set_time_id",
            "set_relationship_state_id",
        }:
            raise ValueError(
                f"Theater story {path} is missing seed continuity baseline"
            )
        # 上面的形态校验已经保证已声明值是精确字符串；这里不再强转或
        # 正规化，目录引用必须与作者写入的稳定 ID 逐字相同。
        time_id = delta.get("set_time_id", "")
        relationship_id = delta.get("set_relationship_state_id", "")
        if time_id and time_id not in time_ids:
            raise ValueError(f"Theater story {path} references unknown continuity time")
        if relationship_id and relationship_id not in relationship_ids:
            raise ValueError(
                f"Theater story {path} references unknown continuity relationship"
            )

    _validate_continuity_stage_references(story, path, nodes)
    _validate_continuity_time_paths(story, path, nodes, edges, time_points)


def _validate_runtime_ledger_contract(
    story: dict[str, Any],
    path: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """校验 Story v3 必需的 theater_ledger_v2 运行时账本。"""  # noqa: DOCSTRING_CJK
    marker = story.get("runtime_ledger_schema_version")
    if marker != _RUNTIME_LEDGER_SCHEMA_VERSION:
        raise ValueError(f"Theater story {path} has invalid runtime ledger schema version")
    events = story.get("events")
    initial = story.get("initial_ledger")
    # 空 Event 目录代表“尚未使用事件账本”的简单剧本；只有节点、边或
    # 初始状态真正引用 Event 时，下面的引用检查才会要求目录提供对应 ID。
    if not isinstance(events, list) or not isinstance(initial, dict):
        raise ValueError(f"Theater story {path} has incomplete runtime ledger contract")
    event_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError(f"Theater story {path} has invalid ledger event")
        event_id = event.get("event_id")
        if not _valid_continuity_id(event_id) or event_id in event_ids:
            raise ValueError(f"Theater story {path} has invalid ledger event id")
        event_ids.add(event_id)
        required = {
            "event_id",
            "event_type",
            "actor_id",
            "visibility",
            "agency",
            "producer_ref",
            "description",
        }
        optional = {
            "object_ref",
            "polarity",
            "modality",
            "public_channel",
            "time_id",
            "location_id",
            "public_evidence_terms",
        }
        if not required.issubset(event) or not set(event).issubset(
            required | optional
        ):
            raise ValueError(f"Theater story {path} has incomplete ledger event")
        if (
            event.get("event_type") not in _LEDGER_EVENT_TYPES
            or event.get("actor_id") not in _LEDGER_EVENT_ACTORS
            or event.get("visibility") not in _LEDGER_EVENT_VISIBILITIES
            or event.get("agency") not in _LEDGER_EVENT_AGENCIES
            or event.get("modality", "actual") not in _LEDGER_EVENT_MODALITIES
            or not _valid_continuity_id(event.get("producer_ref"))
            or not _valid_bounded_text(
                event.get("description"), _LEDGER_EVENT_TEXT_MAX_CHARS
            )
        ):
            raise ValueError(f"Theater story {path} has invalid ledger event")
        channel = event.get("public_channel")
        if channel is not None and channel not in _LEDGER_EVENT_CHANNELS:
            raise ValueError(f"Theater story {path} has invalid ledger event channel")
        if event.get("agency") == "player_commit" and event.get("actor_id") != "player":
            raise ValueError(f"Theater story {path} has invalid player ledger event")
        evidence_terms = event.get("public_evidence_terms", [])
        if (
            not isinstance(evidence_terms, list)
            or len(evidence_terms) > _LEDGER_EVENT_EVIDENCE_MAX_ITEMS
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > _LEDGER_EVENT_EVIDENCE_MAX_CHARS
                for item in evidence_terms
            )
            or len(evidence_terms) != len(set(evidence_terms))
        ):
            raise ValueError(f"Theater story {path} has invalid ledger evidence terms")

    ledger_fields = {
        "world_fact_ids",
        "player_known_event_ids",
        "slot_values",
        "prop_states",
        "revealed_clue_ids",
        "flag_ids",
        "open_loop_states",
        "branch_commitment_ids",
    }
    if set(initial) != ledger_fields:
        raise ValueError(f"Theater story {path} has invalid initial ledger")
    for field in (
        "world_fact_ids",
        "player_known_event_ids",
        "revealed_clue_ids",
        "flag_ids",
        "branch_commitment_ids",
    ):
        value = initial.get(field)
        if not isinstance(value, list) or any(not _valid_continuity_id(item) for item in value):
            raise ValueError(f"Theater story {path} has invalid initial ledger list")
    for field in ("slot_values", "prop_states", "open_loop_states"):
        if not isinstance(initial.get(field), dict):
            raise ValueError(f"Theater story {path} has invalid initial ledger map")
    if any(item not in event_ids for item in initial["player_known_event_ids"]):
        raise ValueError(f"Theater story {path} has unknown initial known event")

    node_ids = {str(node.get("node_id") or "") for node in nodes}
    node_id_list = [str(node.get("node_id") or "") for node in nodes]
    effect_ids: set[str] = set()
    for node in nodes:
        event_refs = node.get("entry_event_ids", [])
        if not isinstance(event_refs, list) or any(
            not _valid_continuity_id(item) or item not in event_ids for item in event_refs
        ):
            raise ValueError(f"Theater story {path} has invalid node event reference")
        node_effects = node.get("node_state_effects", [])
        _validate_state_effects(
            node_effects,
            path=path,
            story=story,
            event_ids=event_ids,
            evidence_event_ids=set(event_refs),
            effect_ids=effect_ids,
            owner=f"node:{node.get('node_id')}",
        )
        continuity_delta = node.get("continuity_delta")
        if isinstance(continuity_delta, dict) and any(
            effect.get("state_kind") in {"time", "relationship"}
            for effect in node_effects
            if isinstance(effect, dict)
        ):
            # 同一节点不能同时由旧 continuity_delta 和 StateEffect 写入同一
            # 类状态，否则恢复时无法判断哪一份是唯一真源。
            raise ValueError(
                f"Theater story {path} mixes continuity delta and state effect"
            )
        acting_beats = node.get("acting_beats")
        if not isinstance(acting_beats, dict):
            continue
        required_delivery = acting_beats.get("must_publish_event_ids", [])
        forbidden_delivery = acting_beats.get("forbidden_claim_event_ids", [])
        if any(
            not isinstance(value, list)
            or len(value) > _LEDGER_EVENT_EVIDENCE_MAX_ITEMS
            or any(not _valid_continuity_id(item) or item not in event_ids for item in value)
            or len(value) != len(set(value))
            for value in (required_delivery, forbidden_delivery)
        ):
            raise ValueError(f"Theater story {path} has invalid node event delivery")
        if set(required_delivery) & set(forbidden_delivery):
            raise ValueError(f"Theater story {path} has conflicting node event delivery")
        for event_id in required_delivery:
            event = next(event for event in events if event.get("event_id") == event_id)
            if (
                event.get("visibility") != "public"
                or event.get("modality", "actual") != "actual"
                or event.get("public_channel") not in {"dialogue", "narration", "transition"}
                or event.get("agency") == "player_commit"
                or not event.get("public_evidence_terms")
            ):
                raise ValueError(f"Theater story {path} has invalid required event delivery")
    for edge in edges:
        trigger = edge.get("trigger")
        refs: list[Any] = []
        trigger_kind = (
            str(trigger.get("kind") or "").strip()
            if isinstance(trigger, dict)
            else ""
        )
        if trigger_kind and trigger_kind not in {"choice", "automatic"}:
            raise ValueError(f"Theater story {path} has invalid edge trigger kind")
        if isinstance(trigger, dict) and "commit_event_id" in trigger:
            if trigger_kind == "automatic":
                raise ValueError(
                    f"Theater story {path} automatic edge contains player commit event"
                )
            refs.append(trigger.get("commit_event_id"))
            commit_id = trigger.get("commit_event_id")
            commit_event = next(
                (event for event in events if event.get("event_id") == commit_id),
                None,
            )
            # agency alone is not proof of ownership: a malformed package
            # must not turn an NPC/环境 action into the player's commitment.
            if (
                not isinstance(commit_event, dict)
                or commit_event.get("agency") != "player_commit"
                or commit_event.get("actor_id") != "player"
            ):
                raise ValueError(f"Theater story {path} has invalid edge commit event")
        transition = edge.get("transition")
        if isinstance(transition, dict):
            transition_refs = transition.get("establishes_event_ids", [])
            if not isinstance(transition_refs, list):
                raise ValueError(f"Theater story {path} has invalid edge event reference")
            refs.extend(transition_refs)
        refs.extend(edge.get("transition_event_ids", []) or [])
        if any(not _valid_continuity_id(item) or item not in event_ids for item in refs):
            raise ValueError(f"Theater story {path} has invalid edge event reference")
        if trigger_kind == "automatic":
            transition_refs = set(refs)
            if not transition_refs:
                raise ValueError(
                    f"Theater story {path} automatic edge has no environment event"
                )
            for event_id in transition_refs:
                event = next(event for event in events if event.get("event_id") == event_id)
                # automatic 只能由环境/NPC 推力触发；即使 agency 被误填为
                # environment，也不能让 actor_id=player 取得这条因果所有权。
                if (
                    event.get("agency") not in {"environment", "npc"}
                    or event.get("actor_id") == "player"
                    or event.get("modality", "actual") != "actual"
                ):
                    raise ValueError(
                        f"Theater story {path} automatic edge has invalid environment event"
                    )
            if not _valid_transition_beat(edge.get("transition_beat")):
                raise ValueError(
                    f"Theater story {path} automatic edge has no transition contract"
                )
        _validate_state_effects(
            edge.get("state_effects", []),
            path=path,
            story=story,
            event_ids=event_ids,
            evidence_event_ids=set(refs),
            effect_ids=effect_ids,
            owner=f"edge:{edge.get('edge_id')}",
        )
        if str(edge.get("from_node") or "") not in node_ids or str(edge.get("to_node") or "") not in node_ids:
            raise ValueError(f"Theater story {path} ledger edge references unknown node")
    # EntryProtocol 是编译器从账本派生的只读投影：未声明它的历史 Story
    # 保持原有导入语义；一旦声明，就必须与当前 Node/Choice/Event 重算结果一致。
    for node in nodes:
        protocol = node.get("entry_protocol")
        issues = fact_lifecycle.validate_entry_protocol(
            protocol,
            node=node,
            events=events,
            edges=edges,
            path=(
                "narrative_nodes["
                f"{node_id_list.index(str(node.get('node_id') or ''))}"
                "].entry_protocol"
            ),
        )
        if issues:
            issue = issues[0]
            raise ValueError(
                f"Theater story {path} {issue.code} at {issue.path}: {issue.message}"
            )
    _validate_ledger_analysis_fields(nodes, path)
    _validate_ledger_prerequisites(
        story,
        path,
        nodes,
        edges,
        event_ids,
    )


def _validate_ledger_analysis_fields(
    nodes: list[dict[str, Any]], path: Path
) -> None:
    """Validate compiler-owned must/may snapshots without treating them as state."""

    analysis_fields = {
        "must_world_state",
        "may_world_state",
        "must_known_event_ids",
        "may_known_event_ids",
        "possible_slot_values",
    }
    for node in nodes:
        present = analysis_fields.intersection(node)
        if not present:
            continue
        if present != analysis_fields:
            raise ValueError(
                f"Theater story {path} has incomplete ledger analysis: {node.get('node_id')}"
            )
        must_world = node["must_world_state"]
        may_world = node["may_world_state"]
        if (
            not isinstance(must_world, dict)
            or set(must_world) != {"fact_ids"}
            or not isinstance(may_world, dict)
            or set(may_world) != {"fact_ids"}
        ):
            raise ValueError(
                f"Theater story {path} has invalid ledger world analysis"
            )
        must_facts = _validated_analysis_ids(must_world["fact_ids"], path)
        may_facts = _validated_analysis_ids(may_world["fact_ids"], path)
        if not set(must_facts).issubset(may_facts):
            raise ValueError(
                f"Theater story {path} has inconsistent ledger world analysis"
            )
        must_known = _validated_analysis_ids(
            node["must_known_event_ids"], path
        )
        may_known = _validated_analysis_ids(node["may_known_event_ids"], path)
        if not set(must_known).issubset(may_known):
            raise ValueError(
                f"Theater story {path} has inconsistent ledger event analysis"
            )
        possible = node["possible_slot_values"]
        if not isinstance(possible, dict):
            raise ValueError(
                f"Theater story {path} has invalid ledger slot analysis"
            )
        for values in possible.values():
            if not isinstance(values, list) or not values:
                raise ValueError(
                    f"Theater story {path} has invalid ledger slot values"
                )


def _validated_analysis_ids(value: Any, path: Path) -> list[str]:
    """Keep derived ID lists stable, unique and safe for downstream prompts."""

    if (
        not isinstance(value, list)
        or any(not _valid_continuity_id(item) for item in value)
        or len(value) != len(set(value))
        or value != sorted(value)
    ):
        raise ValueError(f"Theater story {path} has invalid ledger analysis IDs")
    return value


def _validate_ledger_prerequisites(
    story: dict[str, Any],
    path: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    event_ids: set[str],
) -> None:
    """Recheck compiler must bindings before the Story reaches a Session."""

    node_index = {
        str(node.get("node_id") or ""): node
        for node in nodes
        if str(node.get("node_id") or "")
    }
    incoming: dict[str, list[dict[str, Any]]] = {
        node_id: [] for node_id in node_index
    }
    for edge in edges:
        target_id = str(edge.get("to_node") or "")
        if target_id in incoming:
            incoming[target_id].append(edge)

    scene_index_by_phase = {
        str(scene.get("phase") or ""): (index, scene)
        for index, scene in enumerate(story.get("scenes") or [])
        if isinstance(scene, dict) and str(scene.get("phase") or "")
    }
    initial = story.get("initial_ledger")
    initial_known = set(
        initial.get("player_known_event_ids", [])
        if isinstance(initial, dict)
        else []
    )

    def prerequisite_ids(
        value: Any,
        *,
        owner: str,
        require_known_event: bool,
    ) -> set[str]:
        """Accept only one bounded, exact list; omission represents no gate."""

        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= _LEDGER_EVENT_EVIDENCE_MAX_ITEMS
            or any(not _valid_continuity_id(item) for item in value)
            or len(value) != len(set(value))
        ):
            raise ValueError(
                f"Theater story {path} has invalid must prerequisite: {owner}"
            )
        identifiers = set(value)
        if require_known_event and not identifiers.issubset(event_ids):
            raise ValueError(
                f"Theater story {path} has unknown must prerequisite event: {owner}"
            )
        return identifiers

    def source_must(source_id: str, analysis_field: str, owner: str) -> set[str]:
        """Read a complete source snapshot without deriving state at import."""

        source = node_index.get(source_id, {})
        raw = source.get(analysis_field)
        if analysis_field == "must_world_state":
            raw = raw.get("fact_ids") if isinstance(raw, dict) else None
        if not isinstance(raw, list):
            raise ValueError(
                f"Theater story {path} has missing must prerequisite analysis: {owner}"
            )
        return set(raw)

    def validate_entry(
        value: Any,
        *,
        owner: str,
        target_id: str,
    ) -> None:
        """Require every incoming path to prove a Node or Scene Event gate."""

        required = prerequisite_ids(
            value,
            owner=owner,
            require_known_event=True,
        )
        source_edges = incoming.get(target_id, [])
        if not source_edges:
            # Seed entry happens before its own entry Events; only the initial
            # ledger may satisfy an initial Scene/Node prerequisite.
            if not required.issubset(initial_known):
                raise ValueError(
                    f"Theater story {path} entry prerequisite is not in source must: {owner}"
                )
            return
        for edge in source_edges:
            source_id = str(edge.get("from_node") or "")
            guaranteed = source_must(source_id, "must_known_event_ids", owner)
            # Node entry is checked after its incoming Edge commits. Include
            # only player-commit Events established by that Edge; entry Events
            # on the target Node cannot prove their own prerequisite. Every
            # incoming path still has to prove the complete requirement.
            edge_event_ids: list[str] = []
            trigger = edge.get("trigger")
            if isinstance(trigger, dict):
                commit_event_id = str(trigger.get("commit_event_id") or "").strip()
                if commit_event_id:
                    edge_event_ids.append(commit_event_id)
            for event_id in edge.get("transition_event_ids") or []:
                normalized_event_id = str(event_id or "").strip()
                if normalized_event_id:
                    edge_event_ids.append(normalized_event_id)
            transition = edge.get("transition")
            if isinstance(transition, dict):
                for event_id in transition.get("establishes_event_ids") or []:
                    normalized_event_id = str(event_id or "").strip()
                    if normalized_event_id:
                        edge_event_ids.append(normalized_event_id)
            guaranteed.update(
                event_id
                for event_id in edge_event_ids
                if event_id in event_ids
                and next(
                    (
                        str(event.get("agency") or "")
                        for event in story.get("events") or []
                        if isinstance(event, dict)
                        and event.get("event_id") == event_id
                    ),
                    "",
                ) == "player_commit"
            )
            if not required.issubset(guaranteed):
                raise ValueError(
                    f"Theater story {path} entry prerequisite is not in source must or incoming Edge player_commit events: {owner}"
                )

    for node_index_value, node in enumerate(nodes):
        target_id = str(node.get("node_id") or "")
        if "required_must_known_event_ids" in node:
            validate_entry(
                node["required_must_known_event_ids"],
                owner=(
                    f"narrative_nodes[{node_index_value}]"
                    ".required_must_known_event_ids"
                ),
                target_id=target_id,
            )
        scene_entry = scene_index_by_phase.get(
            str(node.get("belong_phase") or "")
        )
        if scene_entry is not None:
            scene_index, scene = scene_entry
            if "required_must_known_event_ids" in scene:
                validate_entry(
                    scene["required_must_known_event_ids"],
                    owner=(
                        f"scenes[{scene_index}].required_must_known_event_ids"
                    ),
                    target_id=target_id,
                )

    for edge_index, edge in enumerate(edges):
        trigger = edge.get("trigger") if isinstance(edge.get("trigger"), dict) else {}
        choice = trigger.get("choice") if isinstance(trigger, dict) else None
        if not isinstance(choice, dict) or "required_must_fact_ids" not in choice:
            continue
        # Choice 前提绑定来源 Edge，而不是目标节点；这样汇流节点不会把另一条
        # 入路独有事实误当成当前入口的确定事实。
        owner = f"edges[{edge_index}].trigger.choice.required_must_fact_ids"
        required = prerequisite_ids(
            choice["required_must_fact_ids"],
            owner=owner,
            require_known_event=False,
        )
        source_id = str(edge.get("from_node") or "")
        if not required.issubset(source_must(source_id, "must_world_state", owner)):
            raise ValueError(
                f"Theater story {path} choice prerequisite is not in source must: {owner}"
            )


def _validate_state_effects(
    effects: Any,
    *,
    path: Path,
    story: dict[str, Any],
    event_ids: set[str],
    evidence_event_ids: set[str],
    effect_ids: set[str],
    owner: str,
) -> None:
    """验证有限 StateEffect，并把证据限定在当前 Node/Edge 的实际 Event。"""
    if effects is None:
        return
    if not isinstance(effects, list) or len(effects) > _BEAT_LIST_MAX_ITEMS:
        raise ValueError(f"Theater story {path} has invalid state effects: {owner}")
    prop_ids = {
        str(item.get("id") or "")
        for item in story.get("stage_props") or []
        if isinstance(item, dict)
    }
    clue_ids = {
        str(item.get("id") or "")
        for item in story.get("clues") or []
        if isinstance(item, dict)
    }
    for effect in effects:
        if (
            not isinstance(effect, dict)
            or not {"effect_id", "state_kind", "state_id", "op", "evidence_event_id"}.issubset(effect)
            or not set(effect).issubset(_STATE_EFFECT_FIELDS)
        ):
            raise ValueError(f"Theater story {path} has invalid state effect: {owner}")
        effect_id = effect.get("effect_id")
        state_kind = effect.get("state_kind")
        state_id = effect.get("state_id")
        op = effect.get("op")
        evidence_event_id = effect.get("evidence_event_id")
        if (
            not _valid_continuity_id(effect_id)
            or effect_id in effect_ids
            or state_kind not in _STATE_EFFECT_KINDS
            or not _valid_continuity_id(state_id)
            or op not in _STATE_EFFECT_OPS
            or not _valid_continuity_id(evidence_event_id)
            or evidence_event_id not in event_ids
            or evidence_event_id not in evidence_event_ids
        ):
            raise ValueError(f"Theater story {path} has invalid state effect: {owner}")
        if op in {"set", "assert"} and "value" not in effect:
            raise ValueError(f"Theater story {path} state effect is missing value: {owner}")
        if state_kind == "prop" and prop_ids and state_id not in prop_ids:
            raise ValueError(f"Theater story {path} state effect references unknown prop")
        if state_kind == "clue" and clue_ids and state_id not in clue_ids:
            raise ValueError(f"Theater story {path} state effect references unknown clue")
        effect_ids.add(effect_id)


def _valid_continuity_id(value: Any) -> bool:
    """稳定 ID 必须是非空原文，Loader 不替作者静默修剪身份。"""  # noqa: DOCSTRING_CJK
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
    )


def _valid_bounded_text(value: Any, maximum: int) -> bool:
    """目录文字必须完整且有界，不能依赖 Prompt 阶段截断语义。"""  # noqa: DOCSTRING_CJK
    return isinstance(value, str) and 1 <= len(value.strip()) <= maximum


def _valid_time_point(value: Any) -> bool:
    """验证时间点 exact-shape 与可比较序号。"""  # noqa: DOCSTRING_CJK
    if not isinstance(value, dict) or set(value) != {"label", "summary", "sequence"}:
        return False
    sequence = value.get("sequence")
    return (
        _valid_bounded_text(value.get("label"), _CONTINUITY_LABEL_MAX_CHARS)
        and _valid_bounded_text(value.get("summary"), _CONTINUITY_SUMMARY_MAX_CHARS)
        and isinstance(sequence, int)
        and not isinstance(sequence, bool)
        and sequence >= 0
    )


def _valid_relationship_state(value: Any) -> bool:
    """验证关系状态文字与边界列表；关系本身不使用数值进度。"""  # noqa: DOCSTRING_CJK
    if not isinstance(value, dict) or set(value) != {
        "label",
        "summary",
        "boundaries",
    }:
        return False
    boundaries = value.get("boundaries")
    return (
        _valid_bounded_text(value.get("label"), _CONTINUITY_LABEL_MAX_CHARS)
        and _valid_bounded_text(value.get("summary"), _CONTINUITY_SUMMARY_MAX_CHARS)
        and isinstance(boundaries, list)
        and len(boundaries) <= _CONTINUITY_BOUNDARY_MAX_ITEMS
        and all(
            _valid_bounded_text(item, _CONTINUITY_BOUNDARY_MAX_CHARS)
            for item in boundaries
        )
    )


def _validate_continuity_stage_references(
    story: dict[str, Any], path: Path, nodes: list[dict[str, Any]]
) -> None:
    """新包严格绑定既有道具和线索目录，避免自然语言冒充实体 ID。"""  # noqa: DOCSTRING_CJK
    prop_ids = _strict_catalog_ids(story.get("stage_props"), path, "prop")
    clue_ids = _strict_catalog_ids(story.get("clues"), path, "clue")
    node_ids = {str(node.get("node_id") or "") for node in nodes}
    for prop in story.get("stage_props") or []:
        available_from = prop.get("available_from_node")
        if available_from is None or available_from == "":
            continue
        if not _valid_continuity_id(available_from):
            # 运行时按原字符串匹配 node_id；合同不能 trim 后放行一个永远
            # 不会发放道具的节点引用。
            raise ValueError(
                f"Theater story {path} has invalid prop node reference"
            )
        if available_from not in node_ids:
            raise ValueError(f"Theater story {path} references unknown prop node")
    for node in nodes:
        action = node.get("script_action")
        if action is None and "script_action" not in node:
            continue
        if not isinstance(action, dict):
            raise ValueError(f"Theater story {path} has invalid script action")
        for field, known, kind in (
            ("uses_props", prop_ids, "prop"),
            ("reveals_clues", clue_ids, "clue"),
        ):
            if field not in action:
                continue
            references = action.get(field)
            if not isinstance(references, list) or any(
                not _valid_continuity_id(item) for item in references
            ):
                raise ValueError(f"Theater story {path} has invalid {kind} reference")
            if any(item not in known for item in references):
                raise ValueError(f"Theater story {path} references unknown {kind}")


def _strict_catalog_ids(value: Any, path: Path, kind: str) -> set[str]:
    """读取新包既有实体目录；只加强 ID 引用，不改写原 wire shape。"""  # noqa: DOCSTRING_CJK
    if value is None:
        return set()
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Theater story {path} has invalid {kind} catalog")
    if kind == "prop" and any(
        not _valid_bounded_text(item.get("label"), _CONTINUITY_LABEL_MAX_CHARS)
        for item in value
    ):
        # 道具 label 会进入每回合模型快照；只对显式 continuity_v1 新包
        # 要求有界显示名，避免目录输入把内部身份带入运行时。
        raise ValueError(f"Theater story {path} has invalid {kind} catalog")
    if kind == "clue" and any(
        not _valid_bounded_text(
            item.get("title"),
            _CONTINUITY_LABEL_MAX_CHARS,
        )
        or not _valid_bounded_text(
            item.get("public_text"),
            _CONTINUITY_SUMMARY_MAX_CHARS,
        )
        for item in value
    ):
        # Loader 在入口阶段统一限定当前公开字段，避免 Session 已记录
        # “已发现”而模型快照因超限又静默丢失该线索。
        raise ValueError(f"Theater story {path} has invalid {kind} catalog")
    identifiers = [item.get("id") for item in value]
    if any(not _valid_continuity_id(identifier) for identifier in identifiers):
        # ID 会原样进入 Session 与模型显示名索引；先 strip 再校验会制造
        # “合同引用通过、运行时状态找不到”的两个身份。
        raise ValueError(f"Theater story {path} has invalid {kind} catalog")
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"Theater story {path} has invalid {kind} catalog")
    return set(identifiers)


def _validate_continuity_time_paths(
    story: dict[str, Any],
    path: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    time_points: dict[str, Any],
) -> None:
    """传播时间、关系与已出现道具，拒绝倒退、歧义和凭空使用。"""  # noqa: DOCSTRING_CJK
    node_index = {str(node.get("node_id") or ""): node for node in nodes}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_index}
    indegree = {node_id: 0 for node_id in node_index}
    for edge in edges:
        source_id = str(edge.get("from_node") or "")
        target_id = str(edge.get("to_node") or "")
        adjacency[source_id].append(target_id)
        indegree[target_id] += 1

    # 静态图已证明所有节点从 seed 可达且无环；拓扑顺序确保汇流节点在收齐
    # 所有入路状态后再向后传播，而不是偶然采用先遍历到的一条路径。
    pending = [node_id for node_id in node_index if indegree[node_id] == 0]
    possible_time_ids: dict[str, set[str]] = {
        node_id: set() for node_id in node_index
    }
    possible_relationship_ids: dict[str, set[str]] = {
        node_id: set() for node_id in node_index
    }
    seed_id = initial_node_id(story)
    seed_delta = node_index[seed_id]["continuity_delta"]
    possible_time_ids[seed_id].add(str(seed_delta["set_time_id"]))
    possible_relationship_ids[seed_id].add(
        str(seed_delta["set_relationship_state_id"])
    )
    introduced_props: dict[str, set[str]] = {
        node_id: set() for node_id in node_index
    }
    initially_known_props: set[str] = set()
    for prop in story.get("stage_props") or []:
        prop_id = str(prop.get("id") or "")
        available_from = prop.get("available_from_node")
        if available_from in (None, ""):
            initially_known_props.add(prop_id)
        elif available_from in introduced_props:
            introduced_props[available_from].add(prop_id)
    guaranteed_known_props: dict[str, set[str]] = {
        node_id: set() for node_id in node_index
    }
    guaranteed_known_props[seed_id] = (
        initially_known_props | introduced_props[seed_id]
    )
    incoming_known_props: dict[str, list[set[str]]] = {
        node_id: [] for node_id in node_index
    }

    def used_props(node_id: str) -> set[str]:
        """读取已完成形态校验的节点道具引用。"""  # noqa: DOCSTRING_CJK
        action = node_index[node_id].get("script_action")
        return {
            str(item)
            for item in (
                action.get("uses_props")
                if isinstance(action, dict)
                else []
            ) or []
        }

    if not used_props(seed_id).issubset(guaranteed_known_props[seed_id]):
        raise ValueError(
            f"Theater story {path} uses prop before every path introduces it"
        )

    while pending:
        source_id = pending.pop(0)
        source_time_ids = possible_time_ids[source_id]
        source_relationship_ids = possible_relationship_ids[source_id]
        for target_id in adjacency[source_id]:
            incoming_known_props[target_id].append(
                set(guaranteed_known_props[source_id])
            )
            target = node_index[target_id]
            delta = (
                target.get("continuity_delta")
                if isinstance(target.get("continuity_delta"), dict)
                else {}
            )
            target_time_id = str(delta.get("set_time_id") or "")
            if target_time_id:
                target_sequence = int(time_points[target_time_id]["sequence"])
                if any(
                    target_sequence < int(time_points[current_id]["sequence"])
                    for current_id in source_time_ids
                ):
                    raise ValueError(
                        f"Theater story {path} continuity time moves backwards"
                    )
                possible_time_ids[target_id].add(target_time_id)
            else:
                possible_time_ids[target_id].update(source_time_ids)
            target_relationship_id = str(
                delta.get("set_relationship_state_id") or ""
            )
            if target_relationship_id:
                possible_relationship_ids[target_id].add(target_relationship_id)
            else:
                possible_relationship_ids[target_id].update(
                    source_relationship_ids
                )
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                # 同一 sequence 也可以表示两个并行时点，因此汇流必须
                # 比较真实 ID，不能只比较数字。关系状态同理。
                if not target_time_id and len(possible_time_ids[target_id]) > 1:
                    raise ValueError(
                        f"Theater story {path} has ambiguous continuity time merge"
                    )
                if (
                    not target_relationship_id
                    and len(possible_relationship_ids[target_id]) > 1
                ):
                    raise ValueError(
                        f"Theater story {path} has ambiguous continuity relationship merge"
                    )
                predecessor_sets = incoming_known_props[target_id]
                # uses_props 表示“曾使用”，不隐含道具被销毁。
                # 汇流节点只能引用每条真实入路都已出现的道具，
                # 但同一道具可以在后续节点再次使用。
                known_on_every_path = (
                    set.intersection(*predecessor_sets)
                    if predecessor_sets
                    else set()
                )
                known_on_every_path.update(introduced_props[target_id])
                target_used_props = used_props(target_id)
                if not target_used_props.issubset(known_on_every_path):
                    raise ValueError(
                        f"Theater story {path} uses prop before every path introduces it"
                    )
                guaranteed_known_props[target_id] = known_on_every_path
                pending.append(target_id)


def _validate_public_story_contract(story: dict[str, Any], path: Path) -> None:
    """阻止公开背景、初始动作和生成约束重新形成多份作者真源。"""  # noqa: DOCSTRING_CJK
    card = story.get("scenario_card")
    if card is None:
        return
    if not isinstance(card, dict):
        raise ValueError(f"Theater story {path} has invalid scenario card")
    duplicated = {"brief", "rules"}.intersection(card)
    if duplicated:
        raise ValueError(
            f"Theater story {path} scenario card duplicates public or private content"
        )
    for field in ("player_role", "catgirl_role", "primary_goal"):
        if not str(card.get(field) or "").strip():
            raise ValueError(f"Theater story {path} scenario card is missing {field}")


def _validate_static_node_contract(
    story: dict[str, Any],
    nodes: list[dict[str, Any]],
    path: Path,
    scene_phases: set[str],
) -> None:
    """校验 Story v3 节点阶段与表演节拍，禁止运行时补正文或结构字段。"""  # noqa: DOCSTRING_CJK
    seed_nodes = [
        node
        for node in nodes
        if str(node.get("node_type") or "") == "seed"
        and str(node.get("belong_phase") or "") == "setup"
    ]
    if len(seed_nodes) != 1:
        raise ValueError(f"Theater story {path} must have exactly one setup seed")

    allowed_node_types = {"seed", "core", "branch", "ending"}
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        phase = str(node.get("belong_phase") or "").strip()
        node_type = str(node.get("node_type") or "").strip()
        if phase not in scene_phases:
            raise ValueError(
                f"Theater story {path} node references unknown scene phase: {node_id}"
            )
        if node_type not in allowed_node_types:
            raise ValueError(f"Theater story {path} node has invalid type: {node_id}")
        if node_type == "ending" and not str(node.get("ending_id") or "").strip():
            raise ValueError(
                f"Theater story {path} ending node is missing ending_id: {node_id}"
            )
        acting_beats = node.get("acting_beats")
        if not isinstance(acting_beats, dict) or not _valid_acting_beats(acting_beats):
            raise ValueError(
                f"Theater story {path} Story v3 node has invalid acting beats: {node_id}"
            )
        if "pacing" in node and not _valid_node_pacing(node.get("pacing")):
            # relaxed 只能显式表达“不限 stay”；受压节点则必须一次提供完整的
            # 回合上限、逐回合环境事件和唯一强制出口，不能静默忽略半份合同。
            raise ValueError(
                f"Theater story {path} node has invalid pacing: {node_id}"
            )
        suggestions = node.get("suggestions", [])
        if not isinstance(suggestions, list) or suggestions:
            # Story v3 把 Choice 唯一放在来源 Edge；节点 suggestions 会在汇流处制造歧义。
            raise ValueError(
                f"Theater story {path} Story v3 node contains target-owned suggestions: {node_id}"
            )


def _valid_acting_beats(value: Any) -> bool:
    """验证新表演节拍保留作者目标，同时不要求作者预写完整台词。"""  # noqa: DOCSTRING_CJK
    if not isinstance(value, dict):
        return False
    for field in ("narrator_goal", "catgirl_goal", "attitude_to_player"):
        raw_text = value.get(field)
        if not isinstance(raw_text, str):
            return False
        text = raw_text.strip()
        if not text or len(text) > _BEAT_TEXT_MAX_CHARS:
            return False
    for field in ("delivery_tone", "action_cues", "required_terms"):
        items = value.get(field)
        item_limit = (
            _REQUIRED_TERM_MAX_CHARS
            if field == "required_terms"
            else _BEAT_ITEM_MAX_CHARS
        )
        if (
            not isinstance(items, list)
            or len(items) > _BEAT_LIST_MAX_ITEMS
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > item_limit
                for item in items
            )
        ):
            return False
    for field in ("must_publish_event_ids", "forbidden_claim_event_ids"):
        if field not in value:
            continue
        event_ids = value.get(field)
        # 事件身份只允许由作者/编译器传入；具体存在性和公开性由带
        # runtime_ledger_schema_version 的 Story 统一校验。
        if (
            not isinstance(event_ids, list)
            or len(event_ids) > _LEDGER_EVENT_EVIDENCE_MAX_ITEMS
            or any(not _valid_continuity_id(item) for item in event_ids)
            or len(event_ids) != len(set(event_ids))
        ):
            return False
    deliveries = value.get("must_deliver")
    return (
        isinstance(deliveries, list)
        # 节点至少要交付一项剧情信息；否则 Actor 虽有文风提示，
        # 仍缺少作者要求本轮真正推进或确认的语义目标。
        and 1 <= len(deliveries) <= _BEAT_LIST_MAX_ITEMS
        and all(
            isinstance(item, str)
            and item.strip()
            and len(item.strip()) <= _BEAT_ITEM_MAX_CHARS
            for item in deliveries
        )
    )


def _valid_transition_beat(value: Any) -> bool:
    """验证一次转场具备动作与结果，Actor 只负责把两者连贯表达。"""  # noqa: DOCSTRING_CJK
    if not isinstance(value, dict):
        return False
    raw_player_action = value.get("player_action")
    raw_observable_result = value.get("observable_result")
    if not isinstance(raw_player_action, str) or not isinstance(
        raw_observable_result, str
    ):
        return False
    player_action = raw_player_action.strip()
    observable_result = raw_observable_result.strip()
    if (
        not player_action
        or not observable_result
        or len(player_action) > _BEAT_TEXT_MAX_CHARS
        or len(observable_result) > _BEAT_TEXT_MAX_CHARS
    ):
        return False
    for field in ("must_not_repeat",):
        items = value.get(field, [])
        if (
            not isinstance(items, list)
            or len(items) > _BEAT_LIST_MAX_ITEMS
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > _BEAT_ITEM_MAX_CHARS
                for item in items
            )
        ):
            return False
    return True


def _valid_node_pacing(value: Any) -> bool:
    """验证节点节奏的精确形态，保证每次 stay 都有一条有界环境信号。"""  # noqa: DOCSTRING_CJK
    if not isinstance(value, dict):
        return False
    mode = value.get("mode")
    if mode == "relaxed":
        # relaxed 是旧版无限 stay 的显式写法；任何非空附加字段都会制造
        # “看似配置但运行时忽略”的假能力，因此只接受唯一 mode 字段。
        return set(value) == {"mode"}
    required_fields = {
        "mode",
        "max_stay_turns",
        "pressure_beats",
        "forced_edge_id",
        "time_context",
    }
    if mode != "pressured" or set(value) != required_fields:
        return False
    maximum = value.get("max_stay_turns")
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not 1 <= maximum <= 8
    ):
        return False
    beats = value.get("pressure_beats")
    if (
        not isinstance(beats, list)
        or len(beats) != maximum
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item.strip()) > _PACING_TEXT_MAX_CHARS
            for item in beats
        )
    ):
        return False
    forced_edge_id = value.get("forced_edge_id")
    time_context = value.get("time_context")
    return (
        isinstance(forced_edge_id, str)
        and bool(forced_edge_id.strip())
        and isinstance(time_context, str)
        and 1 <= len(time_context.strip()) <= _PACING_TEXT_MAX_CHARS
    )


def _validate_pacing_graph_contract(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    path: Path,
) -> None:
    """证明每个受压节点的强制出口是自己的唯一静态出边。"""  # noqa: DOCSTRING_CJK
    for node in nodes:
        pacing = node.get("pacing") if isinstance(node.get("pacing"), dict) else {}
        if str(pacing.get("mode") or "") != "pressured":
            continue
        node_id = str(node.get("node_id") or "")
        forced_edge_id = str(pacing.get("forced_edge_id") or "")
        matches = [
            edge
            for edge in edges
            if str(edge.get("edge_id") or "") == forced_edge_id
            and str(edge.get("from_node") or "") == node_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Theater story {path} pressured node has invalid forced edge: {node_id}"
            )
        if not _valid_transition_beat(matches[0].get("transition_beat")):
            # 强制出口没有玩家点击产生的旧 callback；必须提供边级节拍，
            # 让 Actor 把环境推力与目标节点一次连贯演完。
            raise ValueError(
                f"Theater story {path} forced edge is missing transition beat: {forced_edge_id}"
            )


def _validate_static_graph_contract(
    story: dict[str, Any],
    path: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """校验全节点可达的前向作者图，并证明推荐边都有对应 Choice。"""  # noqa: DOCSTRING_CJK
    node_index = {str(node.get("node_id") or ""): node for node in nodes}
    outgoing_ids = {
        str(edge.get("from_node") or "") for edge in edges if isinstance(edge, dict)
    }
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if str(node.get("node_type") or "") != "ending" and node_id not in outgoing_ids:
            raise ValueError(
                f"Theater story {path} has non-ending node without outgoing edge: {node_id}"
            )

    # Story v3 的每条边都必须直接携带 trigger；旧版 target-owned Choice
    # 校验不再调用，避免同一条边出现两套所有权解释。
    _validate_edge_choice_transition_edges(edges, path)
    _validate_choice_source_context_contract(story, path, nodes, edges)

    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_index}
    for edge in edges:
        adjacency[str(edge.get("from_node") or "")].append(
            str(edge.get("to_node") or "")
        )
    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(node_id: str) -> None:
        """深度优先证明作者图无环；运行时完成节点过滤不再暗中决定回访语义。"""  # noqa: DOCSTRING_CJK
        if node_id in visiting:
            raise ValueError(f"Theater story {path} static graph contains a cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target_id in adjacency[node_id]:
            _visit(target_id)
        visiting.remove(node_id)
        visited.add(node_id)

    _visit(initial_node_id(story))
    unreachable = sorted(set(node_index) - visited)
    if unreachable:
        # 孤立节点不会被运行时抵达，也可能把环藏在 setup 遍历之外，必须整体拒绝。
        raise ValueError(
            f"Theater story {path} has unreachable static node: {unreachable[0]}"
        )


def _validate_choice_source_context_contract(
    story: dict[str, Any],
    path: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Reject a Choice whose wording only becomes true after the Edge commits.

    A recommended Choice is displayed while the source node and source Scene
    are still active. The target node may describe the consequence, but its
    location and target-only prose cannot leak into the player-facing action.
    This is a structural source/target comparison, not a vocabulary blacklist.
    """

    node_by_id = {
        str(node.get("node_id") or ""): node
        for node in nodes
        if isinstance(node, dict)
    }
    scene_by_phase = {
        str(scene.get("phase") or ""): scene
        for scene in story.get("scenes") or []
        if isinstance(scene, dict)
    }
    for edge in edges:
        trigger = edge.get("trigger") if isinstance(edge, dict) else None
        if not isinstance(trigger, dict) or str(trigger.get("kind") or "") != "choice":
            continue
        choice = trigger.get("choice")
        if not isinstance(choice, dict):
            continue
        source = node_by_id.get(str(edge.get("from_node") or ""), {})
        target = node_by_id.get(str(edge.get("to_node") or ""), {})
        source_scene = scene_by_phase.get(str(source.get("belong_phase") or ""), {})
        target_scene = scene_by_phase.get(str(target.get("belong_phase") or ""), {})
        source_context = _choice_context_text([source, source_scene])
        target_for_context = dict(target)
        target_acting_beats = target.get("acting_beats")
        if isinstance(target_acting_beats, dict):
            # catgirl_goal 会由编译器机械投影当前 Edge Choice 原话，不能反向
            # 把这份派生回声判成目标独有事实；其余表演字段仍参与泄漏检查。
            target_for_context["acting_beats"] = {
                key: value
                for key, value in target_acting_beats.items()
                if key != "catgirl_goal"
            }
        target_context = _choice_context_text([
            target_for_context,
            target_scene,
        ])
        target_location = _choice_context_text(
            (target_scene.get("scene_spec") or {}).get("location_name")
            if isinstance(target_scene.get("scene_spec"), dict)
            else ""
        )
        if not source_context or not target_context:
            continue
        candidates = {
            "label": choice.get("label"),
            "transition_beat.player_action": (
                edge.get("transition_beat", {}).get("player_action")
                if isinstance(edge.get("transition_beat"), dict)
                else None
            ),
        }
        for field, raw_candidate in candidates.items():
            candidate = _choice_context_text(raw_candidate)
            if len(candidate) < 3:
                continue
            target_only = bool(_target_only_choice_overlap(
                candidate,
                source_context,
                target_context,
            ))
            target_only_environment_detail = bool(
                _target_only_environment_detail_overlap(
                    candidate,
                    source_context,
                    target_scene,
                )
            )
            target_only_state_fact = bool(
                _target_only_state_fact_overlap(
                    candidate,
                    source_context,
                    target,
                )
            )
            exposes_target_location = (
                len(target_location) >= 2
                and target_location in candidate
                and target_location not in source_context
            )
            if (
                target_only
                or target_only_environment_detail
                or target_only_state_fact
                or exposes_target_location
            ):
                raise ValueError(
                    f"Theater story {path} choice source context mismatch at "
                    f"edge:{edge.get('edge_id')}.{field}"
                )


def _choice_context_text(value: Any) -> str:
    """Normalize prose only for exact source/target contract comparison."""

    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = (value,)
    text = unicodedata.normalize(
        "NFKC", "".join(str(item or "") for item in values)
    )
    return "".join(
        char
        for char in text
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "S"))
    ).lower()


def _target_only_choice_overlap(
    candidate: str,
    source_context: str,
    target_context: str,
) -> str:
    """Find target-only fact fragments after a Choice is paraphrased.

    Full-string comparison misses ordinary Chinese rewrites such as
    ``核对温湿度计当前读数`` versus ``温湿度计停驻在...``. Four-character
    contiguous fragments keep this a source/target fact comparison rather than
    a cross-story natural-language blacklist.
    """

    if candidate in target_context and candidate not in source_context:
        return candidate
    # Numeric readings are high-confidence target facts. Generic prose such as
    # “公开记录” can legitimately be shared by adjacent nodes and must not be
    # rejected merely because a model paraphrased it.
    if not any(character.isdigit() for character in candidate):
        return ""
    phrases = {
        candidate[index : index + 4]
        for index in range(max(0, len(candidate) - 3))
        if len(candidate[index : index + 4]) == 4
    }
    for phrase in sorted(phrases, key=lambda value: (-len(value), value)):
        if phrase in target_context and phrase not in source_context:
            return phrase
    return ""


def _target_only_environment_detail_overlap(
    candidate: str,
    source_context: str,
    target_scene: dict[str, Any] | None,
) -> str:
    """Find a target-only observable anchor in a Choice.

    ``environment_details`` is a structured Scene-owned fact list. Comparing
    its three-character fragments catches ordinary wording changes such as
    ``狼毫笔墨迹未干`` versus ``拾起狼毫笔`` without maintaining a cross-story
    object blacklist.
    """

    if not isinstance(target_scene, dict):
        return ""
    details = target_scene.get("environment_details")
    if not isinstance(details, list):
        return ""
    for raw_detail in details:
        detail = _choice_context_text(raw_detail)
        if len(detail) < 3:
            continue
        phrases = {
            detail[index : index + 3]
            for index in range(len(detail) - 2)
            if len(detail[index : index + 3]) == 3
        }
        for phrase in sorted(phrases, key=lambda value: (-len(value), value)):
            if phrase in candidate and phrase not in source_context:
                return phrase
    return ""


def _target_only_state_fact_overlap(
    candidate: str,
    source_context: str,
    target_node: dict[str, Any] | None,
) -> str:
    """Find a target-only anchor from the target node's structured facts.

    Scene prose is descriptive output and is too broad to use as a source
    boundary. ``state_diff.add`` is the authored fact ledger for the target
    node, so it is safe to compare its three-character fragments without maintaining a
    cross-story vocabulary blacklist.
    """

    if not isinstance(target_node, dict):
        return ""
    state_diff = target_node.get("state_diff")
    if not isinstance(state_diff, dict):
        return ""
    additions = state_diff.get("add")
    if not isinstance(additions, list):
        return ""
    for raw_fact in additions:
        fact = _choice_context_text(raw_fact)
        if len(fact) < 3:
            continue
        phrases = {
            fact[index : index + 3]
            for index in range(len(fact) - 2)
            if len(fact[index : index + 3]) == 3
        }
        for phrase in sorted(phrases):
            if phrase in candidate and phrase not in source_context:
                return phrase
    return ""


def _validate_edge_choice_transition_edges(
    edges: list[dict[str, Any]], path: Path
) -> None:
    """严格校验 Story v3：每条 Edge 只拥有一个明确的触发合同。"""  # noqa: DOCSTRING_CJK
    choice_ids: set[str] = set()
    for edge in edges:
        edge_id = str(edge.get("edge_id") or "").strip()
        if not edge_id:
            # Edge 已成为 Choice 的归属单位，过渡合同不再允许
            # 依赖数组位置或 target node 猜出一个临时身份。
            raise ValueError(
                f"Theater story {path} edge-choice edge is missing edge_id"
            )
        if _EDGE_CHOICE_LEGACY_EDGE_ROUTING_FIELDS.intersection(edge):
            raise ValueError(
                f"Theater story {path} edge-choice edge contains legacy routing fields: {edge_id}"
            )
        trigger = edge.get("trigger")
        if not isinstance(trigger, dict) or "kind" not in trigger:
            raise ValueError(
                f"Theater story {path} Story v3 edge has invalid trigger: {edge_id}"
            )
        kind = str(trigger.get("kind") or "").strip()
        if kind not in _EDGE_CHOICE_SUPPORTED_TRIGGER_KINDS:
            raise ValueError(
                f"Theater story {path} Story v3 edge has unsupported trigger kind: {edge_id}"
            )
        if kind == "automatic":
            if set(trigger) != {"kind"}:
                raise ValueError(
                    f"Theater story {path} automatic edge has player choice fields: {edge_id}"
                )
            if not _valid_transition_beat(edge.get("transition_beat")):
                raise ValueError(
                    f"Theater story {path} automatic edge has no transition contract: {edge_id}"
                )
            continue
        if set(trigger) != {"kind", "choice", "commit_event_id"}:
            raise ValueError(
                f"Theater story {path} choice edge has incomplete trigger: {edge_id}"
            )
        choice = trigger.get("choice")
        required_choice_fields = {"choice_id", "choice_mode", "label"}
        allowed_choice_fields = required_choice_fields | {
            "completion_phrases",
            "required_must_fact_ids",
        }
        if isinstance(choice, dict) and TARGET_STATE_EVIDENCE_FIELD in choice:
            # Generation proof is intentionally consumed before this boundary;
            # accepting it here would make the authoring DTO a Runtime field.
            raise ValueError(
                f"Theater story {path} Runtime Choice contains authoring-only field: {edge_id}"
            )
        if (
            not isinstance(choice, dict)
            or not required_choice_fields.issubset(choice)
            or not set(choice).issubset(allowed_choice_fields)
        ):
            raise ValueError(
                f"Theater story {path} Story v3 edge has invalid choice: {edge_id}"
            )
        _validate_choice_action_copy_boundary(edge, choice, path)
        _validate_choice_player_action_boundary(edge, choice, path)
        choice_id = str(choice.get("choice_id") or "").strip()
        label = str(choice.get("label") or "").strip()
        choice_mode = str(choice.get("choice_mode") or "").strip()
        completion_phrases = choice.get("completion_phrases", [])
        if (
            not choice_id
            or not label
            or choice_mode not in {"action", "dialogue"}
            or not isinstance(completion_phrases, list)
            or len(completion_phrases) > _BEAT_LIST_MAX_ITEMS
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > _BEAT_ITEM_MAX_CHARS
                for item in completion_phrases
            )
        ):
            raise ValueError(f"Theater story {path} Story v3 edge has invalid choice: {edge_id}")
        if choice_id in choice_ids:
            raise ValueError(
                f"Theater story {path} has duplicate choice id: {choice_id}"
            )
        choice_ids.add(choice_id)
        if not _valid_transition_beat(edge.get("transition_beat")):
            raise ValueError(
                f"Theater story {path} Story v3 edge has no transition contract: {edge_id}"
            )


def _validate_choice_action_copy_boundary(
    edge: dict[str, Any], choice: dict[str, Any], path: Path
) -> None:
    """Keep an action Choice separate from its post-commit observable result."""

    if str(choice.get("choice_mode") or "") != "action":
        return
    label = _choice_context_text(choice.get("label"))
    transition = edge.get("transition_beat")
    result = _choice_context_text(
        transition.get("observable_result")
        if isinstance(transition, dict)
        else ""
    )
    if (
        len(label) >= 4
        and len(result) >= 4
        and (result in label or label in result)
    ):
        raise ValueError(
            f"Theater story {path} choice action contains transition result: "
            f"edge:{edge.get('edge_id')}"
        )


def _validate_choice_player_action_boundary(
    edge: dict[str, Any], choice: dict[str, Any], path: Path
) -> None:
    """确认转场摘要没有替玩家提交按钮之外的新行为。"""  # noqa: DOCSTRING_CJK

    label = _choice_context_text(choice.get("label"))
    transition = edge.get("transition_beat")
    player_action = _choice_context_text(
        transition.get("player_action")
        if isinstance(transition, dict)
        else ""
    )
    if not label or not player_action:
        return
    # 与作者端使用同一条局部证据规则：常规文案比较连续四字，
    # 极短按钮比较完整文案，不维护猫娘题材或动作类型黑名单。
    anchor_size = min(4, len(label))
    if anchor_size < 2:
        return
    if any(
        label[index : index + anchor_size] in player_action
        for index in range(len(label) - anchor_size + 1)
    ):
        return
    raise ValueError(
        f"Theater story {path} choice transition player action mismatch: "
        f"edge:{edge.get('edge_id')}"
    )


def _validate_reachable_ending(story: dict[str, Any], path: Path) -> None:
    """确保作者静态图至少存在一条从开场抵达落幕的路径。"""  # noqa: DOCSTRING_CJK
    adjacency: dict[str, list[str]] = {}
    for edge in story.get("edges") or []:
        adjacency.setdefault(str(edge.get("from_node") or ""), []).append(
            str(edge.get("to_node") or "")
        )
    nodes = {
        str(node.get("node_id") or ""): node
        for node in story.get("narrative_nodes") or []
        if isinstance(node, dict)
    }
    start = initial_node_id(story)
    pending = [start]
    visited: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        pending.extend(adjacency.get(node_id, []))
    reachable_ending = any(
        node_id in visited
        and (str(node.get("node_type") or "") == "ending" or not adjacency.get(node_id))
        for node_id, node in nodes.items()
    )
    if not reachable_ending:
        raise ValueError(f"Theater story {path} has no reachable ending")
