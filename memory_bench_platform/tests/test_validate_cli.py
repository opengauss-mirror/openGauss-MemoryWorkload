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
