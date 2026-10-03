"""Independent, bounded hot-memory allowance for fictional performances."""

import json

from utils.llm_client import is_theater_memory_message, messages_to_dict
from utils.tokenize import count_tokens


THEATER_MEMORY_BUDGET_TOKENS = 6000


def bound_theater_history(history: list) -> list:
    """Keep newest theater capsules within their own budget; preserve ordinary rows.

    Count metadata as well as content, since titles and ending lists also reach
    the prompt. Oversized capsules remain available in the cold archive.
    """
    selected = set()
    used = 0
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if not is_theater_memory_message(message):
            selected.add(index)
            continue
        cost = count_tokens(json.dumps(
            messages_to_dict([message]), ensure_ascii=False, sort_keys=True,
        ))
        if used + cost <= THEATER_MEMORY_BUDGET_TOKENS:
            selected.add(index)
            used += cost
    return [message for index, message in enumerate(history) if index in selected]
