from pathlib import Path

from memory_bench_platform.backends import validate_openviking_source


def test_validate_openviking_source_returns_suggested_config(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='ov'\n", encoding="utf-8")
    payload = validate_openviking_source(
        str(tmp_path),
        api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
        api_key="k",
        vlm_model="doubao-seed-2.0-pro",
        embedding_model="doubao-embedding-vision",
    )
    assert payload["status"] == "ok"
    assert payload["checks"]["README.md"] is True
    assert payload["suggested_config"]["vlm"]["provider"] == "volcengine"
