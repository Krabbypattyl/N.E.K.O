"""Regression tests for bounded structured theater prompts."""

import json

from utils.tokenize import count_tokens

from services.theater.llm_context import bound_prompt_messages


def test_bound_prompt_messages_caps_serialized_json_without_breaking_structure():
    payload = {
        f"very_long_dynamic_key_{index:03d}": "short value"
        for index in range(80)
    }
    messages = [
        {"role": "system", "content": "System rules " * 80},
        {
            "role": "user",
            "content": "以下 JSON：\n" + json.dumps(payload, ensure_ascii=False),
        },
    ]

    bounded = bound_prompt_messages(
        messages,
        max_tokens=120,
        field_max_tokens=40,
    )

    total_tokens = sum(count_tokens(str(message["content"])) for message in bounded)
    assert total_tokens <= 120
    json_start = bounded[1]["content"].find("{")
    assert json_start >= 0
    assert isinstance(json.loads(bounded[1]["content"][json_start:]), dict)


def test_bound_prompt_messages_caps_long_json_values_and_keeps_json_valid():
    messages = [{
        "role": "user",
        "content": "payload=" + json.dumps(
            {"history": ["一段很长的上下文" * 200 for _ in range(12)]},
            ensure_ascii=False,
        ),
    }]

    bounded = bound_prompt_messages(messages, max_tokens=32, field_max_tokens=180)

    content = bounded[0]["content"]
    assert count_tokens(content) <= 32
    assert isinstance(json.loads(content[content.find("{"):]), dict)
