"""Report rubric scores and export Workload answers for the upstream evaluator."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
from statistics import mean


def write_reports(run_dir: Path) -> dict:
    reports = run_dir / "reports"
    cases = json.loads((run_dir / "records/cases.json").read_text(encoding="utf-8"))
    results = json.loads((reports / "case_results.json").read_text(encoding="utf-8"))
    by_case = {item["case_id"]: item for item in results}
    scores = defaultdict(list)
    sample_scores = defaultdict(lambda: defaultdict(list))
    samples = defaultdict(lambda: defaultdict(list))
    for case in cases:
        reference = case["reference"]
        if reference.get("evaluation_target") != "qa_answer":
            continue
        category = reference["category"]
        result = by_case.get(case["case_id"], {})
        response = result.get("response")
        missing_answer = (
            result.get("label") == "missing-answer"
            and type(result.get("score")) in (int, float)
            and result["score"] == 0
            and (response is None or isinstance(response, str) and not response.strip())
        )
        if result.get("score") is not None and not missing_answer:
            try:
                details = json.loads(result.get("rationale") or "{}")
            except json.JSONDecodeError:
                details = {}
            if not isinstance(details, dict) or details.get("profile") != "beam_official@1":
                raise ValueError("Official BEAM report requires beam_official@1 scores; rejudge saved answers first")
        scores[category].append(result.get("score"))
        sample_scores[reference["sample_id"]][category].append(result.get("score"))
        samples[reference["sample_id"]][category].append({
            "question": reference["question"], "rubric": reference["expected_answer"],
            "llm_response": "" if missing_answer else result.get("response") or "",
        })

    def summarize(values):
        graded = [value for value in values if value is not None]
        return {"total": len(values), "graded": len(graded),
                "mean_score": mean(graded) if graded else None}

    from memory_bench_platform.judge_handlers import aggregate_handler, resolve_handler_config, restore_handler_config
    from memory_bench_platform.loader import load_benchmark_skill
    from memory_bench_platform.paths import SKILLS_ROOT
    contract_path = run_dir / "records/run_contract.json"
    if contract_path.is_file():
        config = json.loads(contract_path.read_text(encoding="utf-8")).get("judge_runtime")
        if not isinstance(config, dict) or not config.get("handler_path"):
            raise ValueError("Archived run contract has no aggregation handler")
        config = restore_handler_config(config, SKILLS_ROOT / "benchmarks/beam")
    else:
        manifest = load_benchmark_skill(SKILLS_ROOT, "beam")
        config = resolve_handler_config(manifest.judging or {}, SKILLS_ROOT / "benchmarks/beam",
                                        (manifest.evaluation or {})["primary_metric"])
    aggregate_result = aggregate_handler(config, [
        {"sample_id": sample, "category": category, "score": value}
        for sample, groups in sample_scores.items()
        for category, values in groups.items() for value in values])
    summary = {"profile": "beam_official@1",
               "official_category_scores": aggregate_result["official_category_scores"],
               "mean_category_score": aggregate_result["value"],
               "aggregation": "mean within each sample/category, then mean across samples",

               **summarize([value for values in scores.values() for value in values]),
               "by_category": {key: summarize(values) for key, values in scores.items()}}
    (reports / "beam_scores.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    export = reports / "beam_official_inputs"
    export.mkdir(exist_ok=True)
    for index, groups in enumerate(samples.values(), start=1):
        for name, fields in (("answers", ("question", "llm_response")),
                             ("probes", ("question", "rubric"))):
            data = {category: [{key: row[key] for key in fields} for row in rows]
                    for category, rows in groups.items()}
            (export / f"sample-{index}-{name}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    summary.update(primary_metric=aggregate_result["primary_metric"],
                   metrics=aggregate_result["metrics"],
                   artifacts=[{"path": str(reports / "beam_scores.json"), "kind": "score_report"}]
                             + [{"path": str(path), "kind": "official_input"} for path in sorted(export.glob("*.json"))])
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("data_path", nargs="?", type=Path,
                        help="Accepted for CLI compatibility; scoring uses run archives")
    print(json.dumps(write_reports(parser.parse_args().run_dir), indent=2))
