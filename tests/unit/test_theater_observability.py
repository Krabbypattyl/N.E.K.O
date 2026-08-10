"""验证自由模式模型调用观测只保留当前真实执行链。"""  # noqa: DOCSTRING_CJK

from services.theater import observability


def test_free_actor_calls_have_one_observation_bucket():
    """自由模式只记录一次 Actor 调用，不再保留旧剧本 Repair 指标。"""  # noqa: DOCSTRING_CJK
    observability.reset_evaluation_window()

    # 使用脱敏的固定调用样本，只验证当前执行链的职责分类，不发送真实模型请求。
    observability.record_model_call(
        call_type="theater_free_actor",
        surface="opening",
        started_at=observability.start_timer(),
        status="success",
    )
    observability.record_result(
        responsibility="theater_free_actor",
        surface="opening",
        result_kind="generation",
        outcome="generation_success",
    )

    report = observability.evaluation_report()

    assert report["by_call_type"]["theater_free_actor"]["calls"] == 1
    assert "theater_focus_plan" not in report["by_call_type"]
    assert "theater_focus_repair" not in report["by_call_type"]
    assert report["result_counts"]["generation:opening"] == {
        "generation_success": 1
    }
    assert report["rates"]["fallback_rate"] == 0.0

    # 清理进程内评测窗口，避免该测试的样本污染其它测试的统计断言。
    observability.reset_evaluation_window()
