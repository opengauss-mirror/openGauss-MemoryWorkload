from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.test_entrypoints.collect_run_artifacts import collect_artifacts
from tools.test_entrypoints.probe_remote_env import parse_openclaw_config, parse_openviking_config
from tools.test_entrypoints.reset_remote_locomo_env import build_reset_plan


def test_parse_openclaw_config_reads_gateway_token_and_state_dir():
    data = {
        "gateway": {"port": 18789, "auth": {"token": "abc"}},
        "stateDir": "/root/.openclaw",
    }
    result = parse_openclaw_config(data)
    assert result["gateway_port"] == 18789
    assert result["gateway_token"] == "abc"
    assert result["state_dir"] == "/root/.openclaw"


def test_parse_openviking_config_reads_server_and_judge_fields():
    data = {
        "server": {"port": 1933, "root_api_key": "root-key"},
        "vlm": {"api_base": "https://example.test/v1", "model": "judge-model"},
    }
    result = parse_openviking_config(data)
    assert result["port"] == 1933
    assert result["root_api_key"] == "root-key"
    assert result["judge_base_url"] == "https://example.test/v1"
    assert result["judge_model"] == "judge-model"


def test_build_reset_plan_uses_run_id_in_backup_name():
    result = build_reset_plan("run-123")
    assert result["run_id"] == "run-123"
    assert result["backup_path"].endswith("run-123_backup.tar.gz")
    assert result["ov_data_dir"] == "/root/.openviking/data"


def test_collect_artifacts_copies_files_and_directories(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    file_path = source_dir / "meta.json"
    file_path.write_text('{"ok": true}', encoding="utf-8")
    nested_dir = source_dir / "logs"
    nested_dir.mkdir()
    (nested_dir / "run.log").write_text("done\n", encoding="utf-8")

    output_dir = tmp_path / "collected"
    collected = collect_artifacts(output_dir, [file_path, nested_dir])

    assert len(collected) == 2
    assert (output_dir / "meta.json").read_text(encoding="utf-8") == '{"ok": true}'
    assert (output_dir / "logs" / "run.log").read_text(encoding="utf-8") == "done\n"
