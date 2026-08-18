"""Memory backend adapter interfaces used by LoCoMo evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import openviking_backend


@dataclass(frozen=True)
class IngestSessionAcceptance:
    session_id: str
    task_id: str
    commit_result: dict


@dataclass(frozen=True)
class IngestCompletion:
    session_id: str
    token_usage: dict | None
    wait_diag: dict
    consistency: dict | None
    event: str


@dataclass(frozen=True)
class RecallResult:
    question: str
    target_uri: str
    memories: list[dict]
    total: int


class OpenVikingMemoryBackend:
    """Adapter boundary between LoCoMo flow and OpenViking-specific APIs."""

    def __init__(
        self,
        *,
        api_url: str,
        state_dir: str,
        user_id: str,
        agent_id: str,
        keep_recent_count: int | None = None,
        commit_session_fn: Callable[..., dict] = openviking_backend.commit_openviking_session,
        query_task_usage_fn: Callable[..., dict | tuple[dict | None, dict] | None] = openviking_backend.query_ov_task_token_usage,
        wait_latest_task_fn: Callable[..., dict | tuple[dict | None, dict] | None] = openviking_backend.wait_for_ov_latest_task,
        query_session_usage_fn: Callable[..., dict | None] = openviking_backend.query_ov_session_usage,
        consistency_fn: Callable[..., dict | None] = openviking_backend.query_ov_index_consistency,
        search_memories_fn: Callable[..., list[dict]] = openviking_backend.query_ov_search_find_memories,
        search_total_fn: Callable[..., int] = openviking_backend.query_ov_search_find_total,
    ) -> None:
        self.api_url = api_url
        self.state_dir = state_dir
        self.user_id = user_id
        self.agent_id = agent_id
        self.keep_recent_count = keep_recent_count
        self._commit_session = commit_session_fn
        self._query_task_usage = query_task_usage_fn
        self._wait_latest_task = wait_latest_task_fn
        self._query_session_usage = query_session_usage_fn
        self._consistency = consistency_fn
        self._search_memories = search_memories_fn
        self._search_total = search_total_fn

    @property
    def memory_root_uri(self) -> str:
        return f"viking://user/{self.user_id}/memories"

    def accept_ingest_session(
        self,
        session_id: str,
        *,
        fallback_agent_id: str | None = None,
        wait: bool = False,
    ) -> IngestSessionAcceptance:
        commit_result = self._commit_session(
            ov_api_url=self.api_url,
            session_id=session_id,
            keep_recent_count=self.keep_recent_count,
            wait=wait,
            state_dir=self.state_dir,
            fallback_agent_id=fallback_agent_id or self.agent_id,
        )
        return IngestSessionAcceptance(
            session_id=session_id,
            task_id=str(commit_result.get("task_id") or ""),
            commit_result=commit_result,
        )

    def wait_ingest_completion(
        self,
        *,
        session_id: str,
        task_id: str = "",
        fallback_agent_id: str | None = None,
        max_wait: int = 60,
    ) -> IngestCompletion:
        usage = None
        wait_diag = {
            "poll_count": 0,
            "elapsed_seconds": 0.0,
            "timed_out": False,
            "fallback_used": False,
            "final_status": "no_task",
        }
        agent = fallback_agent_id or self.agent_id
        if task_id:
            usage, wait_diag = self._query_task_usage(
                self.api_url,
                task_id,
                state_dir=self.state_dir,
                fallback_agent_id=agent,
                max_wait=max_wait,
                resource_id=session_id,
                return_diag=True,
            )
        if openviking_backend.is_empty_ov_token_usage(usage):
            usage, wait_diag = self._wait_latest_task(
                self.api_url,
                resource_id=session_id,
                state_dir=self.state_dir,
                fallback_agent_id=agent,
                max_wait=max_wait,
                return_diag=True,
            )
        consistency = self._consistency(
            self.api_url,
            self.memory_root_uri,
            state_dir=self.state_dir,
            fallback_agent_id=self.agent_id,
        )
        event = "completed"
        if wait_diag.get("timed_out"):
            event = "timeout"
        elif usage is None:
            event = "completed_empty"
        return IngestCompletion(
            session_id=session_id,
            token_usage=usage,
            wait_diag=wait_diag,
            consistency=consistency,
            event=event,
        )

    def recall_for_question(
        self,
        question: str,
        *,
        target_uri: str | None = None,
        fallback_agent_id: str | None = None,
        limit: int = 3,
        include_memories: bool = True,
        include_total: bool = True,
    ) -> RecallResult:
        uri = target_uri or self.memory_root_uri
        memories = (
            self._search_memories(
                self.api_url,
                question,
                uri,
                state_dir=self.state_dir,
                fallback_agent_id=fallback_agent_id or self.agent_id,
                limit=limit,
            )
            if include_memories
            else []
        )
        total = (
            self._search_total(
                self.api_url,
                question,
                uri,
                state_dir=self.state_dir,
                fallback_agent_id=fallback_agent_id or self.agent_id,
                limit=limit,
            )
            if include_total
            else len(memories)
        )
        return RecallResult(question=question, target_uri=uri, memories=memories, total=int(total or 0))

    def check_consistency(
        self,
        *,
        target_uri: str | None = None,
        fallback_agent_id: str | None = None,
    ) -> dict | None:
        return self._consistency(
            self.api_url,
            target_uri or self.memory_root_uri,
            state_dir=self.state_dir,
            fallback_agent_id=fallback_agent_id or self.agent_id,
        )

    def read_task_usage(
        self,
        *,
        session_id: str,
        task_id: str = "",
        fallback_agent_id: str | None = None,
        max_wait: int = 30,
    ) -> dict:
        usage = None
        if task_id:
            usage = self._query_task_usage(
                self.api_url,
                task_id,
                state_dir=self.state_dir,
                fallback_agent_id=fallback_agent_id or self.agent_id,
                max_wait=max_wait,
                resource_id=session_id,
            )
        source = "task"
        if openviking_backend.is_empty_ov_token_usage(usage):
            usage = self._query_session_usage(
                self.api_url,
                session_id,
                state_dir=self.state_dir,
                fallback_agent_id=fallback_agent_id or self.agent_id,
                max_wait=max_wait,
                interval=1.0,
            )
            source = "session_meta"
        return {"usage": usage, "source": source}
