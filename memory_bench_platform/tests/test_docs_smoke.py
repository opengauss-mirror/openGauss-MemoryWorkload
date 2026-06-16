from pathlib import Path


def test_readme_mentions_mvp_matrix():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "LoCoMo" in text
    assert "LongMemEval" in text
    assert "OpenClaw" in text
    assert "Generic CLI Agent" in text
    assert "Result Analysis" in text
    assert "latest official release tag" in text
    assert "explicitly requests an allowed override" in text
    assert "concrete runtime version" in text


def test_architecture_doc_mentions_version_policy():
    text = Path("../docs/memory-benchmark-platform-architecture.md").read_text(encoding="utf-8")
    assert "latest_official_release_tag" in text
    assert "version_policy" in text
    assert "resolution_order" in text
    assert "targets" in text
    assert "targets[].version_source" in text
    assert "targets[].upstream" in text
    assert "显式指定允许的 override" in text
    assert "实际运行版本" in text


def test_ovtest_skill_docs_mention_latest_release_tag_policy():
    for path in [
        Path("skills/benchmarks/ovtest-memory/SKILL.md"),
        Path("skills/benchmarks/ovtest-health/SKILL.md"),
        Path("skills/benchmarks/ovtest-admin-memory/SKILL.md"),
    ]:
        text = path.read_text(encoding="utf-8")
        assert "latest_official_release_tag" in text
        assert "version_source" in text
        assert "上游仓库位置" in text


def test_agent_and_benchmark_skill_docs_require_upstream_for_latest_tag_policy():
    for path in [
        Path("skills/agents/openclaw/SKILL.md"),
        Path("skills/agents/hermes/SKILL.md"),
        Path("skills/agents/generic-cli/SKILL.md"),
        Path("skills/benchmarks/locomo/SKILL.md"),
        Path("skills/benchmarks/longmemeval/SKILL.md"),
    ]:
        text = path.read_text(encoding="utf-8")
        assert "latest_official_release_tag" in text
        assert "version_source" in text
        assert "upstream" in text or "上游仓库" in text
        assert "override" in text
