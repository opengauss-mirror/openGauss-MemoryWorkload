from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/benchmarks/production-http-replay/scripts/run_replay.py"
)
VALIDATE_SCRIPT = SCRIPT.with_name("validate.py")


def _module():
    spec = importlib.util.spec_from_file_location("production_replay_runner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_row(path: Path, request: dict, response: dict | None = None):
    path.write_text(
        json.dumps({"request": request, "response": response or {}}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def _valid_dataset(path: Path) -> tuple[Path, Path]:
    add = path / "openmem_add_sample.jsonl"
    search = path / "openmem_search_sample.jsonl"
    _write_row(add, {"user_id": "", "messages": []})
    _write_row(search, {"user_id": "", "query": "needle"})
    return add, search


def test_discover_dataset_requires_one_regular_file_per_operation(tmp_path: Path):
    add, search = _valid_dataset(tmp_path)
    dataset = _module().discover_dataset(tmp_path)
    assert dataset.add_path == add
    assert dataset.search_path == search


@pytest.mark.parametrize(
    "extra_name",
    ["second-add.JSONL", "contains_add_and_search.jsonl"],
)
def test_discover_dataset_rejects_ambiguous_operation_files(
    tmp_path: Path, extra_name: str
):
    _valid_dataset(tmp_path)
    _write_row(tmp_path / extra_name, {"user_id": ""})
    with pytest.raises(ValueError, match="ambiguous"):
        _module().discover_dataset(tmp_path)


def test_discover_dataset_rejects_missing_search(tmp_path: Path):
    _write_row(tmp_path / "openmem_add_sample.jsonl", {"user_id": ""})
    with pytest.raises(ValueError, match="search"):
        _module().discover_dataset(tmp_path)


def test_discover_dataset_ignores_nested_files_and_rejects_symlinks(tmp_path: Path):
    add, search = _valid_dataset(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    _write_row(nested / "another_add.jsonl", {"user_id": ""})
    (tmp_path / "linked-add.jsonl").symlink_to(add)
    dataset = _module().discover_dataset(tmp_path)
    assert dataset.add_path == add
    assert dataset.search_path == search


def test_discover_dataset_accepts_uppercase_suffix(tmp_path: Path):
    add = tmp_path / "requests-ADD.JSONL"
    search = tmp_path / "requests-SEARCH.JSONL"
    _write_row(add, {"user_id": ""})
    _write_row(search, {"user_id": ""})
    assert _module().discover_dataset(tmp_path).add_path == add


def test_iter_records_preserves_payload_and_replaces_only_empty_user(tmp_path: Path):
    path = tmp_path / "openmem_add_sample.jsonl"
    request = {
        "user_id": "",
        "messages": [{"role": "user", "content": "secret"}],
    }
    _write_row(path, request)
    record = next(_module().iter_replay_records(path, "add", "run-7"))
    assert record.request["user_id"] == "replay-run-7"
    assert record.request["messages"][0]["content"] == "secret"
    assert request["user_id"] == ""
    assert record.request_id.startswith("add-000001-")
    assert record.payload_size_bytes > 0
    assert record.top_level_fields == ("messages", "user_id")


def test_iter_records_preserves_nonempty_user_and_stable_hash(tmp_path: Path):
    path = tmp_path / "openmem_search_sample.jsonl"
    _write_row(path, {"query": "needle", "user_id": "existing"})
    first = next(_module().iter_replay_records(path, "search", "run-1"))
    second = next(_module().iter_replay_records(path, "search", "run-2"))
    assert first.request["user_id"] == "existing"
    assert first.request_id == second.request_id
    assert first.payload_sha256 == second.payload_sha256


@pytest.mark.parametrize("content", ["\nnot-secret-valid-json\n", "42\n"])
def test_iter_records_reports_line_without_echoing_bad_content(
    tmp_path: Path, content: str
):
    path = tmp_path / "openmem_search_sample.jsonl"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        list(_module().iter_replay_records(path, "search", "run-7"))
    assert path.name in str(exc.value)
    assert "line 1" in str(exc.value)
    assert "not-secret-valid-json" not in str(exc.value)


def test_baseline_contains_only_content_free_metadata(tmp_path: Path):
    path = tmp_path / "openmem_search_sample.jsonl"
    _write_row(
        path,
        {"query": "REQUEST_SECRET"},
        {
            "status": "success",
            "code": 200,
            "data": {"memory_detail_list": [{"content": "RESPONSE_SECRET"}]},
        },
    )
    record = next(_module().iter_replay_records(path, "search", "run-7"))
    baseline_text = json.dumps(record.baseline)
    assert record.baseline["result_count"] == 1
    assert "REQUEST_SECRET" not in baseline_text
    assert "RESPONSE_SECRET" not in baseline_text


def test_validate_cli_streams_counts_and_shapes(tmp_path: Path):
    add, search = _valid_dataset(tmp_path)
    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "ok",
        "source_path": str(tmp_path),
        "add_file": add.name,
        "search_file": search.name,
        "add_count": 1,
        "search_count": 1,
        "request_shapes": {
            "add": [["messages", "user_id"]],
            "search": [["query", "user_id"]],
        },
    }
