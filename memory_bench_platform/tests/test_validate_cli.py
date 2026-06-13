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
