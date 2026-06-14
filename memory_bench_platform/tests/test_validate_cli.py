import json

from memory_bench_platform.cli import main


def test_validate_cli_returns_locomo_benchmark_status(capsys):
    main(["validate", "--benchmark", "locomo"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["benchmark"]["status"] == "ok"


def test_validate_cli_returns_generic_cli_agent_status(capsys):
    main(["validate", "--agent", "generic-cli"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["agent"]["status"] == "ok"
    assert payload["agent"]["agent"] == "generic-cli"


def test_validate_cli_reports_missing_source_for_longmemeval(capsys):
    main(["validate", "--benchmark", "longmemeval"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["benchmark"]["status"] == "missing_source"
