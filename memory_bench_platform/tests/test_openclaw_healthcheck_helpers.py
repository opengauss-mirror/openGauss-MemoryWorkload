from skills.agents.openclaw.scripts.healthcheck import parse_version_tuple, version_gte


def test_parse_version_tuple_understands_openclaw_version_string():
    assert parse_version_tuple("OpenClaw 2026.3.12 (6472949)") == (2026, 3, 12)


def test_version_gte_handles_minimum_plugin_requirement():
    assert version_gte((2026, 4, 8), (2026, 4, 8)) is True
    assert version_gte((2026, 3, 12), (2026, 4, 8)) is False
