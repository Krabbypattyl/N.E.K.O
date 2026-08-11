"""Regression tests for Numeric v2 time-anchor boundaries."""

from services.theater.time_anchor_contract import numeric_time_anchor_issues


def test_time_anchor_does_not_match_30_days_inside_130_days():
    assert numeric_time_anchor_issues("合同剩余130天，另一个条款是31天。") == []


def test_time_anchor_still_rejects_mixed_deadline_numbers():
    issues = numeric_time_anchor_issues("合同期限还剩30天，停运倒计时31天。")

    assert issues and issues[0]["code"] == "inconsistent_time_anchor"
