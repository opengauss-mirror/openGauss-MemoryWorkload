# LoCoMo Sample Setup Cases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one LoCoMo QA per case while ingesting every sample's sessions once in a setup case that all QA cases explicitly depend on.

**Architecture:** Add ordered case dependencies to the native workflow protocol. A LoCoMo builder emits one non-judged setup case per sample, chains session ingest and task-specific poll steps inside it, then emits the existing QA cases with `depends_on_cases` pointing to that setup. The executor gates dependent cases on setup execution success while preserving one judge result per QA.

**Tech Stack:** Python 3.11+, Pydantic, pytest, native workflow memory/poll operators.

## Global Constraints

- Preserve all pre-existing uncommitted work in the worktree.
- Keep one QA as one judged case; do not change the LoCoMo scoring prompt or correctness criteria.
- Setup cases use `judge_mode: none` and must not create QA result rows.
- Session ingest must be followed by task-specific polling before the next session or any QA executes.
- Agent input contains only the question and OpenViking recall evidence, never the complete conversation.
- Builder output must declare `source_kind: native_workflow`.

---

### Task 1: Ordered Case Dependencies

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/protocol.py`
- Modify: `memory_bench_platform/memory_bench_platform/workflow_inputs.py`
- Test: `memory_bench_platform/tests/test_workflow_inputs.py`

**Interfaces:**
- Produces: `CaseRecord.depends_on_cases: list[str]`.
- Produces: validation requiring every case dependency to exist and appear earlier in the case list.

- [ ] **Step 1: Write failing validation tests**

```python
def test_validate_workflow_accepts_dependency_on_earlier_case():
    setup = _case("sample-setup")
    qa = _case("sample-q1", depends_on_cases=["sample-setup"])
    validate_workflow(cases=[setup, qa], steps=[], execution_spec=ExecutionSpec(), memory_id=None)


def test_validate_workflow_rejects_unknown_case_dependency():
    qa = _case("sample-q1", depends_on_cases=["missing-setup"])
    with pytest.raises(ValueError, match="depends on unknown case"):
        validate_workflow(cases=[qa], steps=[], execution_spec=ExecutionSpec(), memory_id=None)


def test_validate_workflow_rejects_future_case_dependency():
    qa = _case("sample-q1", depends_on_cases=["sample-setup"])
    setup = _case("sample-setup")
    with pytest.raises(ValueError, match="earlier case"):
        validate_workflow(cases=[qa, setup], steps=[], execution_spec=ExecutionSpec(), memory_id=None)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `cd memory_bench_platform && PYTHONPATH=. pytest -q tests/test_workflow_inputs.py -k 'case_dependency'`

Expected: failures because `CaseRecord` does not accept or validate `depends_on_cases`.

- [ ] **Step 3: Implement the minimal protocol and validation**

Add to `CaseRecord`:

```python
depends_on_cases: list[str] = Field(default_factory=list)
```

In `validate_workflow`, validate each dependency against a `case_positions` map and require its position to be lower than the current case.

- [ ] **Step 4: Run the tests and confirm GREEN**

Run: `cd memory_bench_platform && PYTHONPATH=. pytest -q tests/test_workflow_inputs.py -k 'case_dependency'`

Expected: all selected tests pass.

### Task 2: Dependency Gating and Non-Judged Setup Cases

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/workflow.py`
- Test: `memory_bench_platform/tests/test_workflow_executor.py`

**Interfaces:**
- Consumes: `CaseRecord.depends_on_cases`.
- Produces: dependent steps with `status="skipped"` when setup execution did not pass.
- Produces: no `JudgeResult` for `judge_mode in {"none", "external"}`.

- [ ] **Step 1: Write failing executor tests**

```python
def test_workflow_runs_qa_after_successful_setup_case(...):
    setup = CaseRecord(..., case_id="sample-setup", judge_mode="none")
    qa = CaseRecord(..., case_id="sample-q1", depends_on_cases=["sample-setup"])
    output = execute_cases(...)
    assert [item.step_id for item in output["step_results"]] == ["setup-step", "qa-step"]
    assert [item.case_id for item in output["judge_results"]] == ["sample-q1"]


def test_workflow_skips_qa_when_setup_case_fails(...):
    setup = CaseRecord(..., case_id="sample-setup", judge_mode="none")
    qa = CaseRecord(..., case_id="sample-q1", depends_on_cases=["sample-setup"])
    output = execute_cases(...)
    qa_result = next(item for item in output["step_results"] if item.step_id == "qa-step")
    assert qa_result.status == "skipped"
    assert "sample-setup" in qa_result.gate_detail
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `cd memory_bench_platform && PYTHONPATH=. pytest -q tests/test_workflow_executor.py -k 'setup_case'`

Expected: failures because the executor neither gates cases nor honors `judge_mode=none`.

- [ ] **Step 3: Implement execution-status tracking**

Track `case_execution_status: dict[str, str]`. Before a case starts, check all `depends_on_cases`; when any dependency is not `passed`, emit skipped results for that case's steps and mark the case skipped. After normal execution, mark the case passed only when no hard gate failed. Run the builtin judge only for `judge_mode == "builtin"`.

- [ ] **Step 4: Run the tests and confirm GREEN**

Run: `cd memory_bench_platform && PYTHONPATH=. pytest -q tests/test_workflow_executor.py -k 'setup_case'`

Expected: all selected tests pass.

### Task 3: LoCoMo Native Setup and QA Workflows

**Files:**
- Modify: `memory_bench_platform/skills/benchmarks/locomo/scripts/build_tasks.py`
- Modify: `memory_bench_platform/tests/test_locomo_task_builder.py`

**Interfaces:**
- Produces: one `{sample_id}-setup` case per sample.
- Produces: session ingest/poll steps in chronological session order.
- Produces: one `{sample_id}-q{index}` QA case per non-category-5 question, depending on the setup case.
- Produces: recall then agent steps per QA, with `reference.expected_step_id` targeting the agent step.

- [ ] **Step 1: Replace filesystem-dependent builder tests with focused temporary datasets**

Build a fixture containing two sessions and two QA entries, one of which is category 5. Assert:

```python
assert payload["source_kind"] == "native_workflow"
assert [case["case_id"] for case in payload["cases"]] == ["conv-1-setup", "conv-1-q1"]
assert payload["cases"][0]["judge_mode"] == "none"
assert payload["cases"][1]["depends_on_cases"] == ["conv-1-setup"]
assert [step["operator_kind"] for step in payload["steps"]] == [
    "memory", "poll", "memory", "poll", "memory", "agent"
]
assert payload["cases"][1]["reference"]["expected_step_id"] == "conv-1-q1-agent-answer"
```

Also assert that the agent inputs do not contain either session's raw conversation text and do contain a reference to recall evidence.

- [ ] **Step 2: Run the tests and confirm RED**

Run: `cd memory_bench_platform && PYTHONPATH=. pytest -q tests/test_locomo_task_builder.py`

Expected: failures because the current builder emits one agent-only case per QA and sends the complete conversation.

- [ ] **Step 3: Implement the minimal builder change**

Enumerate every `session_N` key dynamically. For each session, emit:

```python
memory ingest -> poll status until completed
```

Chain each session ingest after the previous session poll. For each QA, emit:

```python
memory recall(question) -> agent(question + recalled evidence)
```

The QA case depends on the sample setup case. Keep `judge_mode="builtin"` and target the QA's agent step so existing per-QA result extraction remains unchanged.

- [ ] **Step 4: Run the tests and confirm GREEN**

Run: `cd memory_bench_platform && PYTHONPATH=. pytest -q tests/test_locomo_task_builder.py`

Expected: all builder tests pass.

### Task 4: Regression Verification

**Files:**
- Test only; do not modify unrelated code.

- [ ] **Step 1: Run targeted native workflow tests**

Run:

```bash
cd memory_bench_platform
PYTHONPATH=. pytest -q \
  tests/test_locomo_task_builder.py \
  tests/test_workflow_inputs.py \
  tests/test_workflow_executor.py \
  tests/test_workflow_poll.py \
  tests/test_protocol_models.py
```

Expected: all selected tests pass.

- [ ] **Step 2: Run the complete platform suite**

Run: `cd memory_bench_platform && PYTHONPATH=. pytest -q`

Expected: all tests pass, or any pre-existing environment-only failures are reported separately with exact evidence.

- [ ] **Step 3: Inspect the scoped diff**

Run:

```bash
git diff --check
git diff -- \
  memory_bench_platform/memory_bench_platform/protocol.py \
  memory_bench_platform/memory_bench_platform/workflow_inputs.py \
  memory_bench_platform/memory_bench_platform/workflow.py \
  memory_bench_platform/skills/benchmarks/locomo/scripts/build_tasks.py \
  memory_bench_platform/tests/test_workflow_inputs.py \
  memory_bench_platform/tests/test_workflow_executor.py \
  memory_bench_platform/tests/test_locomo_task_builder.py
```

Expected: no whitespace errors and every changed line maps to the approved LoCoMo setup dependency design.
