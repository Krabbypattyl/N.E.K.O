"""提供自由模式展示所需的角色称谓投影。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

import re
from typing import Any


def render_story_text(value: Any, lanlan_name: str) -> str:
    """把自由模式背景中的角色占位符投影为当前猫娘和玩家称谓。"""  # noqa: DOCSTRING_CJK
    normalized_name = str(lanlan_name or "猫娘").strip() or "猫娘"
    # 这里只服务自由模式开场背景，不再提供 Story v3 节点、Edge 或 Choice 查询。
    return re.sub(
        r"(?:当前)?猫娘[ \t]*\{\{lanlan_name\}\}|\{\{lanlan_name\}\}|当前猫娘|男主|猫娘",
        lambda match: "你" if match.group(0) == "男主" else normalized_name,
        str(value or ""),
    ).strip()
