from pathlib import Path


def test_readme_mentions_mvp_matrix():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "LoCoMo" in text
    assert "LongMemEval" in text
    assert "OpenClaw" in text
    assert "Generic CLI Agent" in text
