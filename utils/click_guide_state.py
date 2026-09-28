"""Independent click-guide progress. Never changes the seven-day tutorial state."""

from pathlib import Path
from threading import RLock

from utils.file_utils import atomic_write_json
from utils.prompt_state.core import load_state_file
from utils.seven_day_tutorial_state import load_seven_day_tutorial_store

_LOCK = RLock()


def _path(config_manager):
    return Path(config_manager.get_config_path("click_guide_state.json"))


def get_click_guide_state(*, config_manager):
    with _LOCK:
        state = load_state_file(_path(config_manager))
        if isinstance(state, dict) and state.get("version") == 1:
            return state
        old = load_seven_day_tutorial_store(config_manager).get("state") or {}
        existing = bool(old.get("completedRounds") or old.get("skippedRounds")
                        or old.get("lastAutoShownRound") or old.get("currentRound"))
        state = {"version": 1, "revision": 0, "choice": "seven-day" if existing else None,
                 "status": "unseen", "pending": False}
        atomic_write_json(_path(config_manager), state, ensure_ascii=False, indent=2)
        return state


def update_click_guide_state(payload, *, config_manager):
    with _LOCK:
        state = get_click_guide_state(config_manager=config_manager)
        revision = payload.get("expectedRevision")
        if type(revision) is not int or revision != state["revision"]:
            return {"ok": False, "state": state}
        action = payload.get("action")
        if action == "choose" and payload.get("choice") in ("click", "seven-day"):
            state.update(choice=payload["choice"], pending=payload["choice"] == "click")
        elif action == "reset":
            state.update(status="unseen", pending=True)
        elif action == "finish" and payload.get("status") in ("completed", "skipped"):
            state.update(status=payload["status"], pending=False)
        else:
            raise ValueError("Invalid click-guide action")
        state["revision"] += 1
        atomic_write_json(_path(config_manager), state, ensure_ascii=False, indent=2)
        return {"ok": True, "state": state}
