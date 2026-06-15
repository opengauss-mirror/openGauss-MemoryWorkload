import json
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "tools" / "test_entrypoints" / "ov_phasea_enrich.py"
SPEC = importlib.util.spec_from_file_location("ov_phasea_enrich", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
enrich_phasea_meta = MODULE.enrich_phasea_meta


class _FakeResponse:
    def __init__(self, payload: dict, ok: bool = True):
        self._payload = payload
        self.ok = ok

    def json(self):
        return self._payload


def test_enrich_phasea_meta_backfills_session_and_task(monkeypatch, tmp_path: Path):
    meta_path = tmp_path / "phaseA_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "ingest_sessions": [
                    {
                        "index": 1,
                        "locomo_session_key": "session_1",
                        "ov_observation": {"detail": {}},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    master_log_path = tmp_path / "run.master.log"
    master_log_path.write_text(
        "[phaseA][session 1/4][direct-ov] session_1 task=11111111-1111-1111-1111-111111111111 session_id=22222222-2222-2222-2222-222222222222 memories=3\n",
        encoding="utf-8",
    )

    def fake_get(url: str, headers: dict[str, str], timeout: int):
        if url.endswith("/api/v1/sessions/22222222-2222-2222-2222-222222222222"):
            return _FakeResponse(
                {
                    "result": {
                        "session_id": "22222222-2222-2222-2222-222222222222",
                        "created_at": "2026-06-15T18:00:00Z",
                        "updated_at": "2026-06-15T18:00:05Z",
                        "llm_token_usage": {"total_tokens": 10},
                    }
                }
            )
        if url.endswith("/api/v1/tasks/11111111-1111-1111-1111-111111111111"):
            return _FakeResponse(
                {
                    "result": {
                        "task_id": "11111111-1111-1111-1111-111111111111",
                        "status": "completed",
                        "result": {
                            "telemetry_summary": {
                                "operation": "session_commit_phase2",
                                "duration_ms": 1234.5,
                            }
                        },
                    }
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr(MODULE.requests, "get", fake_get)

    result = enrich_phasea_meta(
        meta_path=meta_path,
        master_log_path=master_log_path,
        base_url="http://127.0.0.1:1933",
        api_key="k",
        account_id="acct",
        user_id="user",
        agent_id="agent",
    )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    detail = meta["ingest_sessions"][0]["ov_observation"]["detail"]
    assert result["patched_sessions"] == 1
    assert result["patched_tasks"] == 1
    assert detail["session_id"] == "22222222-2222-2222-2222-222222222222"
    assert detail["_ov_task"]["task_id"] == "11111111-1111-1111-1111-111111111111"
    assert detail["telemetry_summary"]["duration_ms"] == 1234.5
