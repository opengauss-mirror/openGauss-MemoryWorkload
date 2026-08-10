from pathlib import Path
import importlib.util
import json

from memory_bench_platform import cli as cli_module


def test_cli_run_smoke_writes_expected_artifacts(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    def _fake_execute(smoke_id: str, run_dir: Path):
        assert smoke_id == "mini-smoke"
        assert run_dir.name == "smoke-run-1"
        return {
            "manifest": {"id": smoke_id},
            "probe": {"stages": ["session_bootstrap"]},
            "validation": {
                "status": "passed",
                "stage_results": [
                    {"case_id": "stage-session_bootstrap", "passed": True, "label": "passed", "question": "session_bootstrap"}
                ],
                "issues": [],
            },
            "report": {"html": "<html>smoke ok</html>"},
        }

    monkeypatch.setattr(cli_module, "execute_smoke_skill", _fake_execute)

    cli_module.main(["run-smoke", "--smoke", "mini-smoke", "--run-id", "smoke-run-1"])

    run_dir = tmp_path / "runs" / "smoke-run-1"
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"] == "passed"
    assert json.loads((run_dir / "reports" / "summary.json").read_text(encoding="utf-8"))["status"] == "passed"
    assert (run_dir / "reports" / "smoke_trace.json").exists()
    assert (run_dir / "reports" / "smoke_summary.json").exists()
    assert (run_dir / "reports" / "smoke_report.html").read_text(encoding="utf-8") == "<html>smoke ok</html>"


def test_smoke_runtime_config_fills_empty_gateway_state_dir(tmp_path: Path):
    script_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "smoke"
        / "locomo-openclaw-openviking-minimal"
        / "scripts"
        / "validate_probe.py"
    )
    spec = importlib.util.spec_from_file_location("validate_probe_for_test", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    data_path = tmp_path / "locomo_test" / "data" / "locomo_small.json"
    data_path.parent.mkdir(parents=True)
    data_path.write_text("[]", encoding="utf-8")
    (config_dir / "mini-test.toml").write_text('[general]\nname = "mini-test"\n', encoding="utf-8")
    (config_dir / "env.toml").write_text('[gateway]\nstate_dir = ""\n', encoding="utf-8")

    runtime_config, _ = module._prepare_runtime_config(
        {"mini_test_config": str(config_dir / "mini-test.toml"), "repo_root": str(tmp_path)},
        tmp_path / "run",
    )

    runtime_env = runtime_config.parent / "env.toml"
    env_text = runtime_env.read_text(encoding="utf-8")
    config_text = runtime_config.read_text(encoding="utf-8")
    assert 'state_dir = "' in env_text
    assert str(runtime_config.parent / "openclaw-state") in env_text
    assert f'data_file = "{data_path}"' in config_text


def test_smoke_static_validate_allows_empty_gateway_state_dir_for_runtime_default(tmp_path: Path):
    script_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "smoke"
        / "locomo-openclaw-openviking-minimal"
        / "scripts"
        / "validate_probe.py"
    )
    spec = importlib.util.spec_from_file_location("validate_probe_for_test_static", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    for name in ("mini-test.toml", "smoke-test.toml", "run_locomo_test_remote.sh"):
        (tmp_path / name).write_text("", encoding="utf-8")
    env_toml = tmp_path / "env.toml"
    env_toml.write_text('[gateway]\nstate_dir = ""\n', encoding="utf-8")

    payload = module._static_validate(
        {
            "mini_test_config": str(tmp_path / "mini-test.toml"),
            "smoke_test_config": str(tmp_path / "smoke-test.toml"),
            "remote_entrypoint": str(tmp_path / "run_locomo_test_remote.sh"),
            "env_toml": str(env_toml),
        }
    )

    assert payload["status"] == "passed"
    assert "gateway_state_dir_empty" not in payload["issues"]


def test_smoke_run_uses_remote_entrypoint_when_available(monkeypatch, tmp_path: Path):
    script_path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "smoke"
        / "locomo-openclaw-openviking-minimal"
        / "scripts"
        / "validate_probe.py"
    )
    spec = importlib.util.spec_from_file_location("validate_probe_for_test_remote", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    probe = {
        "repo_root": str(tmp_path),
        "remote_entrypoint": str(tmp_path / "run_locomo_test_remote.sh"),
        "mini_test_config": str(tmp_path / "mini-test.toml"),
        "smoke_run_name": "mini-smoke",
    }
    Path(probe["remote_entrypoint"]).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    Path(probe["mini_test_config"]).write_text("[general]\nname = \"mini-test\"\n", encoding="utf-8")
    (tmp_path / "env.toml").write_text('[gateway]\nstate_dir = ""\n', encoding="utf-8")

    captured = {}

    def _fake_run(cmd, cwd, env, text, capture_output, check):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(module.subprocess, "run", _fake_run)

    result = module._run_locomo_smoke(probe, tmp_path / "run")

    assert captured["cmd"][0] == "bash"
    assert captured["cmd"][1] == probe["remote_entrypoint"]
    assert captured["env"]["LOCOMO_TEST_CONFIG"] == "mini-test.toml"
    assert captured["env"]["RUN_ID"] == "mini-smoke"
    assert result["output_dir"].endswith("mini-smoke")
