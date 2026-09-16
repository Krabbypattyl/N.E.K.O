"""Evaluator accepts a complete JSON fence without repairing ambiguous output."""

import json

import pytest

from services.theater.numeric_v2_evaluator import (
    NumericV2EvaluatorOutputError,
    _parse_output,
)
from services.theater.numeric_v2_runtime import NumericV2Engine
from tests.unit.test_theater_numeric_v2_contract import numeric_v2_story


# Complete response captured in the ending probe: stop, 74 completion tokens.
RESPONSE = '''{
  "interaction_intent": "chat",
  "history_query": "",
  "public_destination_quote": "",
  "ending_reason": "",
  "scene_complete": false,
  "transition_intent": "unclear",
  "natural_ending_ready": false,
  "metric_changes": {}
}'''


@pytest.fixture
def engine():
    return NumericV2Engine.from_mapping(numeric_v2_story())


@pytest.mark.parametrize('language', ['json', '', 'JSON'])
def test_complete_fence_preserves_evaluation(engine, language):
    plain = _parse_output(RESPONSE, engine, '谢谢，修得真不错。')
    fenced = _parse_output(f' \n```{language}\n{RESPONSE}\n```\n ', engine, '谢谢，修得真不错。')
    assert fenced == plain
    assert not fenced.scene_complete and not fenced.natural_ending_ready
    assert fenced.transition_intent == 'unclear'


@pytest.mark.parametrize('content', [
    f'结果如下：\n```json\n{RESPONSE}\n```',
    f'```json\n{RESPONSE}\n```\n可以继续。',
    f'```json\n{RESPONSE}\n```\n```json\n{RESPONSE}\n```',
    f'```python\n{RESPONSE}\n```',
    f'```json\n{RESPONSE}',
    f'```json\n{RESPONSE[:-1]}\n```',
    '{"ending_reason":"尚未完成',
])
def test_fence_does_not_repair_or_extract_json(engine, content):
    with pytest.raises(NumericV2EvaluatorOutputError, match='evaluator_invalid_json'):
        _parse_output(content, engine, '谢谢。')


@pytest.mark.parametrize('field,value,error', [
    ('scene_complete', 'true', 'scene_complete_invalid'),
    ('natural_ending_ready', 1, 'natural_ending_invalid'),
    ('metric_changes', [], 'changes_invalid'),
    ('transition_intent', 'yes', 'transition_intent_invalid'),
    ('goal_progress', {}, 'fields_invalid'),
])
def test_fence_keeps_field_validation(engine, field, value, error):
    payload = {**json.loads(RESPONSE), field: value}
    with pytest.raises(NumericV2EvaluatorOutputError, match=error):
        _parse_output(f'```json\n{json.dumps(payload)}\n```', engine, '谢谢。')


def test_fence_does_not_authorize_unverified_destination(engine):
    payload = {**json.loads(RESPONSE), 'transition_intent': 'initiate',
               'public_destination_quote': '我们去未公开的房间。'}
    plain = _parse_output(json.dumps(payload), engine, '带路吧。')
    fenced = _parse_output(f'```json\n{json.dumps(payload)}\n```', engine, '带路吧。')
    assert fenced == plain
    assert fenced.transition_intent == 'unclear'
    assert fenced.public_destination_quote == ''
