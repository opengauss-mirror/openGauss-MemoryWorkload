import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "test_entrypoints" / "repair_phasea_csv.py"
SPEC = importlib.util.spec_from_file_location("repair_phasea_csv", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_detect_repair_qis_finds_timeout_and_connection_error_rows():
    rows = [
        {"qi": "1", "response": "ok"},
        {"qi": "2", "response": "Request timed out before a response was generated."},
        {"qi": "3", "response": "[ERROR] ConnectionError"},
        {"qi": "4", "response": "ok"},
    ]
    assert MODULE.detect_repair_qis(rows) == [2, 3]


def test_write_seed_csv_removes_target_qis(tmp_path: Path):
    source = tmp_path / "main.csv"
    source.write_text(
        "\n".join(
            [
                "sample_id,qi,response",
                "conv-1,1,ok",
                "conv-1,2,bad-timeout",
                "conv-1,3,ok",
            ]
        ),
        encoding="utf-8",
    )
    seed = tmp_path / "seed.csv"
    kept = MODULE.write_seed_csv(source, seed, remove_qis=[2])
    assert kept == 2
    text = seed.read_text(encoding="utf-8")
    assert "conv-1,2,bad-timeout" not in text
    assert "conv-1,1,ok" in text
    assert "conv-1,3,ok" in text


def test_merge_replayed_rows_replaces_target_rows_and_writes_backup(tmp_path: Path):
    main = tmp_path / "main.csv"
    main.write_text(
        "\n".join(
            [
                "sample_id,qi,response",
                "conv-1,1,ok",
                "conv-1,2,timeout",
                "conv-1,3,ok",
            ]
        ),
        encoding="utf-8",
    )
    replay = tmp_path / "replay.csv"
    replay.write_text(
        "\n".join(
            [
                "sample_id,qi,response",
                "conv-1,2,fixed",
            ]
        ),
        encoding="utf-8",
    )
    backup_path, replaced = MODULE.merge_replayed_rows(main, replay, replace_qis=[2])
    assert replaced == 1
    assert backup_path is not None
    assert backup_path.exists()
    text = main.read_text(encoding="utf-8")
    assert "conv-1,2,fixed" in text
    assert "conv-1,2,timeout" not in text
