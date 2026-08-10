"""Authoring-only evidence DTO stays separate from the Runtime Choice wire shape."""

import pytest

from services.theater.authoring_dto import ChoiceTargetStateEvidenceDTO


def test_target_state_evidence_dto_preserves_wire_shape_and_exact_matching():
    evidence = ChoiceTargetStateEvidenceDTO.from_wire([
        "事实 A",
        {"kind": "fact", "key": "事实 B"},
    ])

    assert evidence.to_wire() == [
        "事实 A",
        {"kind": "fact", "key": "事实 B"},
    ]
    assert evidence.matches_target_additions([
        "事实 A",
        {"kind": "fact", "key": "事实 B"},
        "事实 C",
    ])
    assert not evidence.matches_target_additions(["事实 A"])


def test_target_state_evidence_dto_rejects_duplicates_and_blank_items():
    with pytest.raises(ValueError, match="target_state_evidence_invalid"):
        ChoiceTargetStateEvidenceDTO.from_wire(["事实 A", "事实 A"])
    with pytest.raises(ValueError, match="target_state_evidence_invalid"):
        ChoiceTargetStateEvidenceDTO.from_wire(["   "])
