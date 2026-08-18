import json

from memory_bench_platform.locomo_test_artifacts import load_locomo_test_artifacts


def test_load_locomo_test_artifacts_reads_meta_rows_and_logs(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    (tmp_path / "qa_diagnostics.json").write_text(json.dumps({"issues": {"x": 1}}), encoding="utf-8")
    (tmp_path / ".ingest_record.json").write_text(json.dumps({"s1": {"timestamp": 1}}), encoding="utf-8")
    (tmp_path / "qa_results.csv").write_text(
        "\n".join(
            [
                "sample_id,qi,question",
                "conv-1,1,Q1",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "pipeline.log").write_text("demo-log", encoding="utf-8")
    (tmp_path / "session_ingest_diagnostics.jsonl").write_text(
        json.dumps({"session_key": "session_1", "status": "passed"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "report.html").write_text("<html></html>", encoding="utf-8")

    bundle = load_locomo_test_artifacts(tmp_path)

    assert bundle.meta["name"] == "demo"
    assert bundle.qa_diagnostics["issues"]["x"] == 1
    assert bundle.ingest_record["s1"]["timestamp"] == 1
    assert bundle.qa_rows[0]["question"] == "Q1"
    assert bundle.pipeline_log == "demo-log"
    assert bundle.session_ingest_diagnostics[0]["session_key"] == "session_1"
    assert bundle.artifact_paths()["report_html"].endswith("report.html")
