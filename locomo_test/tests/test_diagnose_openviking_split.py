from pathlib import Path
import importlib.util


def _load_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "tools/test_entrypoints/diagnose_openviking_split.py"
    )
    spec = importlib.util.spec_from_file_location("diagnose_openviking_split", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_json_payload_accepts_clean_json():
    module = _load_module()
    payload = module.parse_json_payload('{"status":"ok","count":1}\n')
    assert payload == {"status": "ok", "count": 1}


def test_parse_json_payload_skips_leading_warnings():
    module = _load_module()
    text = "WARNING something noisy\nWARNING another line\n{\n  \"status\": \"ok\",\n  \"count\": 2\n}\n"
    payload = module.parse_json_payload(text)
    assert payload == {"status": "ok", "count": 2}


def test_parse_json_payload_returns_none_for_non_json():
    module = _load_module()
    assert module.parse_json_payload("warning only\nstill no json\n") is None
