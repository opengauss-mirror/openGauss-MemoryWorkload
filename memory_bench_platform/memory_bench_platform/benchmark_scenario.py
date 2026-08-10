from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ScenarioQuestion(BaseModel):
    question_id: str
    question: str
    reference: Any = None
    category: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScenarioEvaluation(BaseModel):
    target: Literal[
        "qa_answer",
        "retrieval",
        "memory_extraction",
        "memory_update",
        "agent_action",
    ] = "qa_answer"
    profile: str | None = None
    primary_metric: str | None = None
    questions: list[ScenarioQuestion] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TimelineEvent(BaseModel):
    event_id: str
    type: Literal["conversation", "document", "trajectory", "feedback", "checkpoint"]
    timestamp: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    evaluation: ScenarioEvaluation | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "TimelineEvent":
        if self.type == "checkpoint" and self.evaluation is None:
            raise ValueError("checkpoint event requires evaluation")
        if self.type != "checkpoint" and self.evaluation is not None:
            raise ValueError("only checkpoint events may declare evaluation")
        return self


class ScenarioSample(BaseModel):
    sample_id: str
    namespace_hint: str | None = None
    timeline: list[TimelineEvent] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_checkpoint(self) -> "ScenarioSample":
        if not any(event.type == "checkpoint" for event in self.timeline):
            raise ValueError("scenario sample requires at least one checkpoint")
        return self


class BenchmarkScenario(BaseModel):
    source_kind: Literal["benchmark_scenario"] = "benchmark_scenario"
    benchmark_id: str
    requirements: dict[str, Any] = Field(default_factory=dict)
    evaluation: ScenarioEvaluation = Field(default_factory=ScenarioEvaluation)
    samples: list[ScenarioSample] = Field(default_factory=list)
    execution_spec: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_unique_sample_ids(self) -> "BenchmarkScenario":
        sample_ids = [sample.sample_id for sample in self.samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("scenario sample_id values must be unique")
        return self


class RunBinding(BaseModel):
    benchmark_id: str
    agent_id: str
    agent_runtime_id: str | None = None
    agent_local: bool = False
    memory_id: str | None = None
    memory_integration: Literal["backend_direct", "agent_plugin"] = "backend_direct"
    memory_plugin_id: str | None = None
    run_id: str
