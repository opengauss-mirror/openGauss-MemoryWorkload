"""Versioned, JSON-compatible multi-request Judge extension contract."""
import inspect
import hashlib
import runpy
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["judge-handler/1"] = "judge-handler/1"
    reference: dict[str, Any]
    answer: str
    prompt_template: str


class JudgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["judge-handler/1"]
    score: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    rationale: str



class AggregationRow(BaseModel):
    sample_id: str
    category: str
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)


class AggregationRequest(BaseModel):
    protocol_version: Literal["judge-handler/1"] = "judge-handler/1"
    rows: list[AggregationRow]


class AggregationResponse(BaseModel):
    protocol_version: Literal["judge-handler/1"]
    primary_metric: str
    value: float | None = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    metrics: list[dict[str, Any]]


def resolve_handler_config(judging, skill_root, primary_metric):
    from pathlib import Path
    root = Path(skill_root).resolve()
    config = {**judging, "primary_metric": primary_metric}
    template = config.get("prompt_template")
    if template:
        path = (root / template).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Invalid benchmark judge prompt template")
        config["prompt_template_text"] = path.read_text(encoding="utf-8")
    if config.get("handler"):
        path = (root / config["handler"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Invalid benchmark judge handler")
        config["handler"] = path.relative_to(root).as_posix()
        config["handler_skill"] = root.name
        config["handler_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        load_handler(path, config.get("handler_protocol"))
        config["handler_path"] = str(path)
    return config



def restore_handler_config(config, skill_root):
    """Resolve archived selectors only inside the caller's installed trusted Skill."""
    from pathlib import Path
    root = Path(skill_root).resolve()
    restored = dict(config)
    if config.get("handler_skill", root.name) != root.name:
        raise ValueError("Archived Judge Skill does not match the installed Skill")
    relative = config.get("handler")
    if relative:
        if Path(relative).is_absolute():
            raise ValueError("Archived Judge handler must use a relative path")
        path = (root / relative).resolve()
    else:
        # Legacy archives are usable only at their original trusted location.
        path = Path(config.get("handler_path") or "").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Archived Judge handler is outside the installed Skill or missing")
    digest = config.get("handler_sha256")
    if digest:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Archived Judge implementation hash does not match installed Skill")
    elif Path(config.get("handler_path") or "").resolve() != path:
        raise ValueError("Legacy Judge archive cannot verify a relocated implementation")
    restored["handler_path"] = str(path)
    return restored

def aggregate_handler(config, rows):
    handler = load_handler(config["handler_path"], config.get("handler_protocol"))
    request = AggregationRequest(rows=rows)
    result = handler["aggregate"](request.model_dump(mode="json"))
    validated = AggregationResponse.model_validate(result).model_dump(mode="json")
    if validated["primary_metric"] != config["primary_metric"]:
        raise ValueError("Judge aggregation primary metric differs from benchmark declaration")
    return {**result, **validated}

def load_handler(path, protocol):
    if protocol != "judge-handler/1":
        raise ValueError("Unsupported Judge handler protocol")
    module = runpy.run_path(str(path))
    if module.get("PROTOCOL_VERSION") != protocol:
        raise ValueError("Judge handler protocol declaration mismatch")
    for name, args in (("evaluate", ({}, lambda *args: "")), ("aggregate", ([],))):
        function = module.get(name)
        if not callable(function):
            raise ValueError("Judge handler requires callable " + name)
        try:
            inspect.signature(function).bind(*args)
        except TypeError as exc:
            raise ValueError("Invalid Judge handler signature: " + name) from exc
    return module


def evaluate_handler(config, reference, answer, invoke):
    handler = load_handler(config["handler_path"], config.get("handler_protocol"))
    request = JudgeRequest(reference=reference, answer=answer,
                           prompt_template=config["prompt_template_text"])
    result = handler["evaluate"](request.model_dump(mode="json"), invoke)
    response = JudgeResponse.model_validate(result)
    return response.score, response.rationale


def apply_primary_metric(summary, cases, judge_results, config):
    if not config.get("handler_path"):
        return summary
    references = {case.case_id: case.reference for case in cases}
    rows = [{"sample_id": references[result.case_id]["sample_id"],
             "category": references[result.case_id]["category"], "score": result.score}
            for result in judge_results if references.get(result.case_id, {}).get("evaluation_target") == "qa_answer"]
    metric = aggregate_handler(config, rows)
    value = metric["value"]
    return {**summary, "primary_metric": metric["primary_metric"],
            "full_score_pass_rate": summary["raw_benchmark_score"],
            "raw_benchmark_score": value, "benchmark_score": value}
