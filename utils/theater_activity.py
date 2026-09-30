# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-memory record of characters whose theater session a client is driving.

This is a cheap server-side backstop for the frontend's own theater guards
(``isProactiveChatSuppressed`` / ``blocksOrdinaryVoice``): proactive chat and
ordinary voice start consult :func:`is_theater_active` so a client that missed
the frontend suppression still cannot interleave ordinary chat with a running
performance.

The signal is deliberately lossy and fails open:

- it is refreshed by successful theater session requests (launch, restore,
  input, resume) and cleared by end, by an ended snapshot, and by the capsule's
  explicit release on exit;
- every entry expires ``THEATER_ACTIVITY_TTL_SECONDS`` after the last refresh,
  so a crashed or closed theater window can never block ordinary voice or
  proactive chat for longer than that;
- it lives only in process memory, so a restart forgets it, and a character
  that never opened the theater never appears here (zero behaviour change).

Lives in ``utils/`` so the theater router, the proactive-chat router and the
WebSocket router can share it without importing each other.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

THEATER_ACTIVITY_TTL_SECONDS = 120.0

# lanlan_name -> monotonic timestamp of the last theater request that proved a
# client was driving that character's session.
_last_activity: dict[str, float] = {}


def _key(lanlan_name: Any) -> str:
    return str(lanlan_name or "").strip()


def mark_theater_activity(lanlan_name: Any, *, now: float | None = None) -> None:
    """Record that a client is currently driving ``lanlan_name``'s theater session."""

    key = _key(lanlan_name)
    if key:
        _last_activity[key] = time.monotonic() if now is None else now


def clear_theater_activity(lanlan_name: Any) -> None:
    """Forget one character's theater activity."""

    _last_activity.pop(_key(lanlan_name), None)


def clear_all_theater_activity() -> None:
    """Forget every character's theater activity (the capsule's explicit release)."""

    _last_activity.clear()


def is_theater_active(lanlan_name: Any, *, now: float | None = None) -> bool:
    """Return True while ``lanlan_name`` had theater activity within the TTL."""

    key = _key(lanlan_name)
    last = _last_activity.get(key) if key else None
    if last is None:
        return False
    current = time.monotonic() if now is None else now
    if current - last < THEATER_ACTIVITY_TTL_SECONDS:
        return True
    # Expired entries are dropped so stale state can never outlive its TTL.
    _last_activity.pop(key, None)
    return False


def note_theater_session_response(response: Any) -> None:
    """Update the registry from a successful theater session payload.

    Only dict payloads with ``ok: True`` carrying both ``session`` and
    ``participants`` count; error responses (``JSONResponse``) are ignored so a
    failed request neither refreshes nor clears the signal.
    """

    if not isinstance(response, Mapping) or response.get("ok") is not True:
        return
    session = response.get("session")
    participants = response.get("participants")
    if not isinstance(session, Mapping) or not isinstance(participants, Mapping):
        return
    name = _key(participants.get("catgirl_name"))
    if not name:
        return
    if session.get("status") == "ended":
        clear_theater_activity(name)
    else:
        mark_theater_activity(name)


__all__ = [
    "THEATER_ACTIVITY_TTL_SECONDS",
    "clear_all_theater_activity",
    "clear_theater_activity",
    "is_theater_active",
    "mark_theater_activity",
    "note_theater_session_response",
]
