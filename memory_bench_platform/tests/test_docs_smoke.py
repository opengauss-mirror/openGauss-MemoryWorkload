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
