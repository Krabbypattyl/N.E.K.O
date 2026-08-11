"""Safe one-pass projection of author names to runtime names."""

from __future__ import annotations

import re
from typing import Any, Iterable


def replace_names(value: Any, replacements: Iterable[tuple[Any, Any]]) -> str:
    """Replace all non-empty source names once, longest names first."""

    text = str(value or "")
    mapping: dict[str, str] = {}
    for source, target in replacements:
        source_text = str(source or "")
        target_text = str(target or "")
        if source_text and source_text != target_text:
            if source_text in mapping and mapping[source_text] != target_text:
                raise ValueError("conflicting_name_replacement")
            mapping[source_text] = target_text
    if not mapping:
        return text
    pattern = re.compile("|".join(
        re.escape(source) for source in sorted(mapping, key=len, reverse=True)
    ))
    return pattern.sub(lambda match: mapping[match.group(0)], text)


__all__ = ["replace_names"]
