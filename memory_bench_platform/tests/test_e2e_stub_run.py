from pathlib import Path
import json

from memory_bench_platform.cli import main


def test_stub_run_creates_run_json_and_summary(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "memory_bench_platform.cli.build_version_selection",
        lambda manifest, overrides=None: {
            "selection_mode": manifest.version_policy.default_selection,
            "overridden": False,
            "targets": [{"name": "stub", "resolved_default": {"status": "resolved", "resolved_version": "v1.2.3"}}],
        },
    )
    main(
        [
            "run",
            "--benchmark",
            "locomo",
            "--agent",
            "generic-cli",
        ]
    )
    runs = list((tmp_path / "runs").glob("*"))
    assert runs, "expected one run directory to be created"
    assert (runs[0] / "reports" / "summary.json").exists()
    assert (runs[0] / "reports" / "case_results.json").exists()
    assert (runs[0] / "reports").is_dir()
    run_record = json.loads((runs[0] / "run.json").read_text(encoding="utf-8"))
    version_selection = json.loads((runs[0] / "records" / "version_selection.json").read_text(encoding="utf-8"))
    assert run_record["benchmark_version_policy"]["default_selection"] == "latest_official_release_tag"
    assert run_record["agent_version_policy"]["default_selection"] == "latest_official_release_tag"
    assert version_selection["benchmark"]["selection_mode"] == "latest_official_release_tag"
    assert version_selection["benchmark"]["overridden"] is False
    assert version_selection["benchmark"]["targets"][0]["resolved_default"]["resolved_version"] == "v1.2.3"
