"""把作者剧本中的双主角名称投影为当前玩家与当前猫娘。"""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping

from .name_projection import replace_names


_NAME_SEPARATOR_RE = re.compile(r"[，,；;：:\n（(]")


def _identity_name(identity: Any) -> str:
    """身份首段是作者角色名；没有独立名字时不做危险的全文猜测。"""  # noqa: DOCSTRING_CJK

    first = _NAME_SEPARATOR_RE.split(str(identity or "").strip(), maxsplit=1)[0].strip()
    if not first or len(first) > 24:
        return ""
    return first


@dataclass(frozen=True, slots=True)
class NumericV2CastProjection:
    """运行时固定：男主由玩家扮演，女主由当前猫娘扮演。"""  # noqa: DOCSTRING_CJK

    source_player_name: str
    source_catgirl_name: str
    player_name: str
    catgirl_name: str

    @classmethod
    def from_story(
        cls,
        story: Mapping[str, Any],
        *,
        player_name: str,
        catgirl_name: str,
    ) -> "NumericV2CastProjection":
        intro = story.get("intro") if isinstance(story.get("intro"), Mapping) else {}
        return cls(
            source_player_name=_identity_name(intro.get("player_identity")),
            source_catgirl_name=_identity_name(intro.get("catgirl_identity")),
            player_name=str(player_name or "你").strip() or "你",
            catgirl_name=str(catgirl_name or "当前猫娘").strip() or "当前猫娘",
        )

    def text(self, value: Any) -> str:
        lead_pair = f"{self.player_name}和{self.catgirl_name}"
        replacements = (
            ("男女主人公", lead_pair),
            ("男女主角", lead_pair),
            ("男女主", lead_pair),
            (self.source_player_name, self.player_name),
            (self.source_catgirl_name, self.catgirl_name),
        )
        return replace_names(value, replacements)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {str(key): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.value(item) for item in value)
        return deepcopy(value)

    def intro(self, story: Mapping[str, Any]) -> dict[str, Any]:
        return self.value(dict(story.get("intro") or {}))


__all__ = ["NumericV2CastProjection"]
