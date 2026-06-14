from locomo_test.config import Config, JudgeEnv
from locomo_test import judge as judge_module


def test_judge_defaults_to_openai_for_volcengine_coding_base(monkeypatch, tmp_path):
    cfg = Config()
    cfg.judge_env = JudgeEnv(
        api_key="token",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
        model="doubao-seed-2.0-pro",
        parallel=1,
    )

    captured = {}

    async def fake_grade_openai(client, model, question, gold, response):
        captured["called"] = "openai"
        return True, "ok"

    monkeypatch.setattr(judge_module, "_grade_openai", fake_grade_openai)
    monkeypatch.setattr(judge_module, "HAS_OPENAI", True)

    class DummyClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise AssertionError("should not reach real API")

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(judge_module, "AsyncOpenAI", DummyClient)

    csv_path = tmp_path / "qa_results.csv"
    csv_path.write_text(
        "sample_id,qi,question,expected,response,category,result,reasoning\n"
        "conv-1,1,Q1,A1,R1,1,,\n",
        encoding="utf-8",
    )

    import asyncio

    asyncio.run(judge_module._run_judge(cfg, str(csv_path)))
    assert captured["called"] == "openai"
