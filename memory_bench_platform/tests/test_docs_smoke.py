from pathlib import Path


def test_readme_mentions_mvp_matrix():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "LoCoMo" in text
    assert "LongMemEval" in text
    assert "OpenClaw" in text
    assert "Generic CLI Agent" in text
    assert "Result Analysis" in text
    assert "latest official release tag" in text


def test_architecture_doc_mentions_version_policy():
    text = Path("../docs/memory-benchmark-platform-architecture.md").read_text(encoding="utf-8")
    assert "latest_official_release_tag" in text
    assert "version_policy" in text
    assert "resolution_order" in text
    assert "targets" in text


def test_ovtest_skill_docs_mention_latest_release_tag_policy():
    for path in [
        Path("skills/benchmarks/ovtest-memory/SKILL.md"),
        Path("skills/benchmarks/ovtest-health/SKILL.md"),
        Path("skills/benchmarks/ovtest-admin-memory/SKILL.md"),
    ]:
        text = path.read_text(encoding="utf-8")
        assert "latest_official_release_tag" in text
