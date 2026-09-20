import json
from pathlib import Path
import pytest

from memory_bench_platform.cli import main


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "locomo_minimal.json"


def test_validate_cli_returns_locomo_benchmark_status(capsys):
    main(["validate", "--benchmark", "locomo", "--data-path", str(FIXTURE)])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["benchmark"]["status"] == "ok"


def test_validate_cli_returns_generic_cli_agent_status(capsys):
    main(["validate", "--agent", "generic-cli"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["agent"]["status"] == "ok"
    assert payload["agent"]["agent"] == "generic-cli"


def test_validate_cli_emits_run_contract_when_benchmark_and_agent_are_provided(capsys):
    main(
        [
            "validate",
            "--benchmark",
            "locomo",
            "--agent",
            "openclaw",
            "--data-path",
            str(FIXTURE),
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["run_contract"]["selection"]["benchmark_id"] == "locomo"
    assert payload["run_contract"]["selection"]["agent_id"] == "openclaw"
    assert payload["run_contract"]["selection"]["memory_id"] == "openviking"


def test_validate_cli_reports_missing_source_for_longmemeval(capsys):
    with pytest.raises(ValueError, match="--data-path is required"):
        main(["validate", "--benchmark", "longmemeval"])


def test_validate_cli_reports_production_replay_counts(tmp_path: Path, capsys):
    (tmp_path / "sample_add.jsonl").write_text(
        json.dumps({"request": {"user_id": "", "messages": []}}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "sample_search.jsonl").write_text(
        json.dumps({"request": {"user_id": "", "query": "needle"}}) + "\n",
        encoding="utf-8",
    )
    main(
        [
            "validate",
            "--benchmark",
            "production-http-replay",
            "--data-path",
            str(tmp_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)["benchmark"]
    assert payload["status"] == "ok"
    assert payload["add_count"] == 1
    assert payload["search_count"] == 1
    assert "needle" not in json.dumps(payload)


def test_readme_documents_production_replay_contract():
    text = Path(__file__).resolve().parents[2].joinpath("README.md").read_text(
        encoding="utf-8"
    )
    for marker in (
        "production-http-replay",
        "openmem-v1",
        "MEMORY_BENCH_REPLAY_ADD_CONCURRENCY",
        "request_events.jsonl",
        "add → drain → search",
        "无法重建生产环境中的读写交错时序",
    ):
        assert marker in text
