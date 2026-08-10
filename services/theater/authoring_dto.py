"""Authoring-only DTOs shared by InkAI generation and N.E.K.O boundaries."""  # noqa: DOCSTRING_CJK

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


TARGET_STATE_EVIDENCE_FIELD = "target_state_evidence"
TARGET_STATE_EVIDENCE_MAX_ITEMS = 8


def _canonical_json(value: Any) -> str:
    """Compare scalar and structured facts without changing their wire shape."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class ChoiceTargetStateEvidenceDTO:
    """Generation/compile proof for an action Choice's target facts.

    The serialized form is the existing ``target_state_evidence`` array. This
    DTO is deliberately not part of the Runtime Edge Choice contract.
    """

    items: tuple[Any, ...] = ()

    @classmethod
    def from_wire(cls, value: Any) -> "ChoiceTargetStateEvidenceDTO":
        if value is None:
            return cls()
        if (
            not isinstance(value, list)
            or len(value) > TARGET_STATE_EVIDENCE_MAX_ITEMS
            or any(not isinstance(item, (str, dict)) for item in value)
            or any(isinstance(item, str) and not item.strip() for item in value)
            or len({_canonical_json(item) for item in value}) != len(value)
        ):
            raise ValueError("target_state_evidence_invalid")
        return cls(tuple(value))

    def to_wire(self) -> list[Any]:
        return list(self.items)

    def matches_target_additions(self, additions: Any) -> bool:
        """Require every proof item to equal a target ``state_diff.add`` item."""

        if not isinstance(additions, list):
            return not self.items
        allowed = {_canonical_json(item) for item in additions}
        return all(_canonical_json(item) in allowed for item in self.items)
