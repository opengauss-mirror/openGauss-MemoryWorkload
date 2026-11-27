"""BEAM official rubric evaluation and LLM-aligned event ordering."""
import json
import math

CATEGORIES = {
    "abstention", "contradiction_resolution", "event_ordering", "information_extraction",
    "instruction_following", "knowledge_update", "multi_session_reasoning",
    "preference_following", "summarization", "temporal_reasoning",
}
EQUIVALENCE_SYSTEM = '\n            You are a binary classifier.\n            If the TWO snippets describe the SAME event/fact, reply **YES**\n            Otherwise reply **NO**. No extra words.\n            DO NOT provide any exaplanation.\n        '
EQUIVALENCE_USER = 'First snippet: <first> \n\n                       Second snippet: <second>\n                    '

def _json_response(text):
    text = text.strip()
    if text.startswith(chr(96) * 3):
        text = text.split("\n", 1)[1].rsplit(chr(96) * 3, 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        from json_repair import repair_json
        return json.loads(repair_json(text))


def _ordering(reference, predicted, invoke):
    used, aligned = set(), []
    for text in predicted:
        for index, fact in enumerate(reference):
            if index in used:
                continue
            prompt = EQUIVALENCE_USER.replace("<first>", fact).replace("<second>", text)
            if "yes" in invoke(prompt, EQUIVALENCE_SYSTEM).lower():
                aligned.append(fact)
                used.add(index)
                break
        else:
            aligned.append(text)

    tp = len(set(reference) & set(aligned))
    fp = len([text for text in aligned if text not in reference])
    fn = len([text for text in reference if text not in aligned])
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    union = list(dict.fromkeys(reference + aligned))
    tie_rank = len(union) + 1
    first = {text: i + 1 for i, text in enumerate(reference)}
    second = {text: i + 1 for i, text in enumerate(aligned)}
    concordant = discordant = tied_first = tied_second = 0
    for i, text in enumerate(union):
        for other in union[i + 1:]:
            a = first.get(text, tie_rank) - first.get(other, tie_rank)
            b = second.get(text, tie_rank) - second.get(other, tie_rank)
            if a * b > 0:
                concordant += 1
            elif a * b < 0:
                discordant += 1
            elif a == 0 and b != 0:
                tied_first += 1
            elif b == 0 and a != 0:
                tied_second += 1
    pairs = concordant + discordant
    denominator = math.sqrt((pairs + tied_first) * (pairs + tied_second))
    if not denominator:
        raise ValueError("BEAM event ordering has undefined Kendall tau")
    tau_norm = ((concordant - discordant) / denominator + 1) / 2
    return dict(precision=precision, recall=recall, f1=f1,
                tau_norm=tau_norm, final_score=tau_norm * f1,
                reference=reference, aligned=aligned)


PROTOCOL_VERSION = "judge-handler/1"


def evaluate(request, invoke):
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unsupported BEAM Judge request protocol")
    reference, answer, template = request["reference"], request["answer"], request["prompt_template"]
    rubric = reference.get("expected_answer")
    category = reference.get("category")
    if category not in CATEGORIES:
        raise ValueError("Unknown BEAM category: " + str(category))
    if not isinstance(rubric, list) or not rubric or any(
        not isinstance(item, str) or not item.strip() for item in rubric
    ):
        raise ValueError("BEAM requires a nonempty list of rubric criteria")
    metrics = _ordering(rubric, answer.split("\n"), invoke) if category == "event_ordering" else {}
    if not isinstance(template, str) or not template.strip():
        raise ValueError("BEAM requires the configured Judge template")
    responses = []
    for item in rubric:
        prompt = template.replace("<rubric_item>", item).replace("<llm_response>", answer)
        response = _json_response(invoke(prompt, None))
        score = response.get("score")
        if type(score) not in (int, float) or score not in (0, 0.5, 1):
            raise ValueError("BEAM criterion score must be numeric 0, 0.5 or 1")
        responses.append(response)
    metrics.update(profile="beam_official@1", llm_judge_score=sum(item["score"] for item in responses) / len(rubric),
                   llm_judge_responses=responses)
    # Upstream report_results.py uses tau_norm for ordering and llm_judge_score elsewhere.
    primary = metrics["tau_norm"] if category == "event_ordering" else metrics["llm_judge_score"]
    return {"protocol_version": PROTOCOL_VERSION, "score": float(primary),
            "rationale": json.dumps(metrics, ensure_ascii=False)}

def aggregate(request):
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unsupported BEAM aggregation protocol")
    rows = request["rows"]
    from collections import defaultdict
    from statistics import mean
    by_sample = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row.get("score") is not None:
            by_sample[row["sample_id"]][row["category"]].append(row["score"])
    categories = defaultdict(list)
    for groups in by_sample.values():
        for category, scores in groups.items():
            categories[category].append(mean(scores))
    values = {category: mean(scores) for category, scores in categories.items()}
    value = mean(values.values()) if values else None
    return {"protocol_version": PROTOCOL_VERSION,
            "primary_metric": "mean_category_score", "value": value,
            "official_category_scores": values,
            "metrics": [{"name": "mean_category_score", "value": value, "scope": "run",
                         "unit": "ratio", "direction": "higher_is_better"}]
                       + [{"name": "category/" + key, "value": score, "scope": "run",
                           "unit": "ratio", "direction": "higher_is_better"} for key, score in values.items()]}
