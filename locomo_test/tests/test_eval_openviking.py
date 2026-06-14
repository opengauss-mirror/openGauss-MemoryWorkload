from locomo_test.eval import normalize_ov_task_query_mode, should_attempt_gateway_compact


def test_normalize_ov_task_query_mode_prefers_direct_ov_stable_for_openviking():
    assert normalize_ov_task_query_mode("openviking") == "direct_ov_stable"


def test_should_attempt_gateway_compact_disabled_for_openviking_by_default(monkeypatch):
    monkeypatch.delenv("LOCOMO_OPENVIKING_FORCE_COMPACT", raising=False)
    assert should_attempt_gateway_compact("openviking") is False


def test_should_attempt_gateway_compact_can_be_forced(monkeypatch):
    monkeypatch.setenv("LOCOMO_OPENVIKING_FORCE_COMPACT", "true")
    assert should_attempt_gateway_compact("openviking") is True
