import json
import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "benchmarks"
    / "locomo"
    / "tooling"
    / "test_entrypoints"
    / "ov_phasea_enrich.py"
)
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


def _fake_search_response(total: int, uris: list[str]) -> _FakeResponse:
    return _FakeResponse(
        {
            "result": {
                "memories": [{"uri": uri} for uri in uris],
                "resources": [],
                "skills": [],
                "total": total,
            }
        }
    )


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

    def fake_post(url: str, headers: dict[str, str], json: dict[str, object], timeout: int):
        assert url.endswith("/api/v1/search/find")
        if json["target_uri"] == "viking://user/user/memories":
            return _fake_search_response(2, ["viking://user/user/memories/events/demo.md"])
        if json["target_uri"] == "viking://agent/memories":
            return _fake_search_response(1, ["viking://agent/locomo-eval/memories/demo.md"])
        raise AssertionError(json)

    monkeypatch.setattr(MODULE.requests, "get", fake_get)
    monkeypatch.setattr(MODULE.requests, "post", fake_post)

    result = enrich_phasea_meta(
        meta_path=meta_path,
        csv_path=None,
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
    assert meta["qa_direct_search_probe"]["question_count"] == 0


def test_enrich_phasea_meta_creates_minimal_meta_when_missing(monkeypatch, tmp_path: Path):
    meta_path = tmp_path / "phaseA_on_2sessions_demo_meta.json"
    csv_path = tmp_path / "phaseA_on_2sessions_demo.csv"
    csv_path.write_text(
        "\n".join(
            [
                "sample_id,sample_idx,qi,question,expected,response,category,evidence,elapsed_seconds,rounds,input_tokens,output_tokens,cacheRead,cacheWrite,total_tokens,timestamp,jsonl_filename,result,reasoning",
                'conv-26,0,2,Q1,E1,R1,1,"[D1:1]",4.5,1,100,20,30,0,150,2026-06-15 10:00:00,,CORRECT,ok',
            ]
        ),
        encoding="utf-8",
    )
    master_log_path = tmp_path / "run.master.log"
    master_log_path.write_text(
        "[phaseA][session 1/2][direct-ov] session_1 task=11111111-1111-1111-1111-111111111111 session_id=22222222-2222-2222-2222-222222222222 memories=3\n",
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
                    }
                }
            )
        if url.endswith("/api/v1/tasks/11111111-1111-1111-1111-111111111111"):
            return _FakeResponse(
                {
                    "result": {
                        "task_id": "11111111-1111-1111-1111-111111111111",
                        "status": "completed",
                        "created_at_iso": "2026-06-15T18:00:00+00:00",
                        "updated_at_iso": "2026-06-15T18:00:06+00:00",
                        "telemetry_summary": {
                            "operation": "session_commit_phase2",
                            "duration_ms": 1500.0,
                        },
                    }
                }
            )
        raise AssertionError(url)

    def fake_post(url: str, headers: dict[str, str], json: dict[str, object], timeout: int):
        assert url.endswith("/api/v1/search/find")
        if json["target_uri"] == "viking://user/user/memories":
            return _fake_search_response(0, [])
        if json["target_uri"] == "viking://agent/memories":
            return _fake_search_response(0, [])
        raise AssertionError(json)

    monkeypatch.setattr(MODULE.requests, "get", fake_get)
    monkeypatch.setattr(MODULE.requests, "post", fake_post)

    result = enrich_phasea_meta(
        meta_path=meta_path,
        csv_path=csv_path,
        master_log_path=master_log_path,
        base_url="http://127.0.0.1:1933",
        api_key="k",
        account_id="acct",
        user_id="user",
        agent_id="agent",
    )

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert result["patched_sessions"] == 1
    assert result["patched_tasks"] == 1
    assert meta["run_id"] == "phaseA_on_2sessions_demo"
    assert len(meta["qa_rows"]) == 1
    assert meta["qa_rows"][0]["usage"]["total_tokens"] == 150
    assert meta["ingest_sessions"][0]["compact_elapsed_seconds"] == 6.0
    assert meta["ingest_sessions"][0]["telemetry_summary"]["duration_ms"] == 1500.0
    assert meta["qa_direct_search_probe"]["question_count"] == 1
    assert meta["qa_direct_search_probe"]["all_zero"] is True
