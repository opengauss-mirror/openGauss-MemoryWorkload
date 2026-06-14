from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .protocol import RunRecord


@dataclass
class RunPlanRequest:
    benchmark_id: str
    agent_id: str
    run_id: str | None = None
    benchmark_version: str | None = None
    agent_version: str | None = None
    memory_backend: str | None = None
    hardware_profile: str | None = None
    data_path: str | None = None


@dataclass
class RunPlan:
    run_id: str
    benchmark_id: str
    agent_id: str
    benchmark_version: str | None
    agent_version: str | None
    memory_backend: str | None
    hardware_profile: str | None
    data_path: str | None

    def to_run_record(self) -> RunRecord:
        return RunRecord(
            run_id=self.run_id,
            benchmark_id=self.benchmark_id,
            agent_id=self.agent_id,
            benchmark_version=self.benchmark_version,
            agent_version=self.agent_version,
            memory_backend=self.memory_backend,
            hardware_profile=self.hardware_profile,
            config={"data_path": self.data_path} if self.data_path else {},
            status="stubbed",
        )


def build_run_plan(request: RunPlanRequest) -> RunPlan:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    run_id = request.run_id or f"{request.benchmark_id}-{request.agent_id}-{stamp}"
    return RunPlan(
        run_id=run_id,
        benchmark_id=request.benchmark_id,
        agent_id=request.agent_id,
        benchmark_version=request.benchmark_version,
        agent_version=request.agent_version,
        memory_backend=request.memory_backend,
        hardware_profile=request.hardware_profile,
        data_path=request.data_path,
    )
