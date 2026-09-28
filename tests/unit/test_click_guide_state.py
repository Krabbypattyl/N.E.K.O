import json

import pytest

from utils.click_guide_state import get_click_guide_state, update_click_guide_state


class Config:
    def __init__(self, root):
        self.root = root

    def get_config_path(self, name):
        return str(self.root / name)


@pytest.mark.unit
def test_new_user_can_choose_and_complete_without_settling_seven_days(tmp_path):
    config = Config(tmp_path)
    state = get_click_guide_state(config_manager=config)
    assert state["choice"] is None
    choice = update_click_guide_state({"action": "choose", "choice": "click", "expectedRevision": 0}, config_manager=config)
    assert choice["state"]["pending"]
    done = update_click_guide_state({"action": "finish", "status": "completed", "expectedRevision": 1}, config_manager=config)
    assert done["state"]["status"] == "completed"
    assert not done["state"]["pending"]
    assert not (tmp_path / "seven_day_tutorial_state.json").exists()


@pytest.mark.unit
@pytest.mark.parametrize("settled", ["completedRounds", "skippedRounds"])
def test_existing_user_is_not_prompted_and_reset_is_independent(tmp_path, settled):
    config = Config(tmp_path)
    old = tmp_path / "seven_day_tutorial_state.json"
    old.write_text(json.dumps({"initialized": True, "revision": 3, "state": {settled: [1, 2]}}))
    before = old.read_bytes()
    state = get_click_guide_state(config_manager=config)
    assert state["choice"] == "seven-day"
    assert not state["pending"]
    reset = update_click_guide_state({"action": "reset", "expectedRevision": 0}, config_manager=config)
    assert reset["state"]["pending"]
    assert reset["state"]["choice"] == "seven-day"
    assert old.read_bytes() == before


@pytest.mark.unit
def test_late_completion_cannot_erase_a_reset_from_another_window(tmp_path):
    config = Config(tmp_path)
    get_click_guide_state(config_manager=config)
    update_click_guide_state({"action": "reset", "expectedRevision": 0}, config_manager=config)
    stale = update_click_guide_state({"action": "finish", "status": "completed", "expectedRevision": 0}, config_manager=config)
    assert stale["ok"] is False
    assert stale["state"]["pending"] is True
    assert stale["state"]["status"] == "unseen"


@pytest.mark.unit
def test_legacy_completed_user_is_not_prompted(tmp_path):
    (tmp_path / "tutorial_prompt.json").write_text(json.dumps({"home_tutorial_completed": True}))
    assert get_click_guide_state(config_manager=Config(tmp_path))["choice"] == "seven-day"


@pytest.mark.unit
def test_invalid_action_does_not_write(tmp_path):
    config = Config(tmp_path)
    before = get_click_guide_state(config_manager=config)
    with pytest.raises(ValueError):
        update_click_guide_state({"action": "finish", "status": "anything", "expectedRevision": 0}, config_manager=config)
    assert get_click_guide_state(config_manager=config) == before
