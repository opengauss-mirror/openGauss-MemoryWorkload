# Memory Auto-Instrumentation Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a repository-owned Skill and reusable prompt that guide a meta agent through safe, request-correlated instrumentation of an unfamiliar memory system.

**Architecture:** Keep this as a guidance-only Skill under `skills/instrumentation`; do not register it with the runtime Integration Skill loader. Contract tests pin the required workflow, canonical stage names, privacy rules, and prompt inputs so later edits cannot weaken the instrumentation procedure.

**Tech Stack:** Markdown, pytest, pathlib

**Spec:** `docs/superpowers/specs/2026-09-20-production-replay-instrumentation-design.md`

## Global Constraints

- The Skill must trace public add/search entry points before proposing edits.
- Category shares use non-overlapping leaf stages; wrapper spans remain diagnostic.
- Instrumentation defaults off and emits structured JSONL when enabled.
- Logs must not contain prompts, messages, queries, memory content, credentials, or private endpoints.
- Error spans must preserve and re-raise the original exception.
- The meta agent must run target-repository tests and a short add/search probe.
- This guidance-only Skill has no `manifest.yaml` and does not enter `load_all_skills()`.

## Review Focus

- A target system without keyword search or rerank should report those stages as absent, not invent spans; Task 1 pins this rule.
- Async code that offloads a blocking backend call needs outer wrapper and inner backend leaf guidance; Task 1 pins both roles.
- Existing telemetry conventions must take precedence over a new helper; Task 1 requires a discovery step before edits.
- The prompt must reject source edits before the call graph and stage map exist; Task 2 checks the ordering.
- Coverage reports must distinguish missing request correlation from zero stage duration; Task 2 requires separate fields.

---

### Task 1: Add the automatic instrumentation Skill contract

**Files:**
- Create: `memory_bench_platform/skills/instrumentation/memory-system-auto-instrumentation/SKILL.md`
- Create: `memory_bench_platform/tests/test_instrumentation_skill_docs.py`

**Interfaces:**
- Consumes: the canonical stage and log contracts in the approved spec.
- Produces: a guidance-only `SKILL.md` with sections `Discovery`, `Stage Map`, `Instrumentation Contract`, `Safety`, `Verification`, and `Coverage Report`.

- [ ] **Step 1: Write the failing document-contract test**

```python
from pathlib import Path


SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills/instrumentation/memory-system-auto-instrumentation/SKILL.md"
)


def test_auto_instrumentation_skill_pins_stage_and_safety_contract():
    text = SKILL.read_text(encoding="utf-8")
    for heading in (
        "## Discovery",
        "## Stage Map",
        "## Instrumentation Contract",
        "## Safety",
        "## Verification",
        "## Coverage Report",
    ):
        assert heading in text
    for stage in (
        "extract.preprocess",
        "extract.llm",
        "extract.vectorize",
        "extract.write",
        "retrieve.query_vectorize",
        "retrieve.vector_search",
        "retrieve.keyword_search",
        "retrieve.fusion",
        "retrieve.rerank",
        "retrieve.format",
    ):
        assert stage in text
    assert "non-overlapping leaf" in text
    assert "wrapper" in text
    assert "request_id" in text
    assert "default off" in text
    assert "re-raise" in text
    assert "Do not log" in text
    assert "absent" in text
    assert "backend_call" in text
```

- [ ] **Step 2: Run the test and confirm the missing-file failure**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_instrumentation_skill_docs.py -v`

Expected: FAIL with `FileNotFoundError` for `memory-system-auto-instrumentation/SKILL.md`.

- [ ] **Step 3: Write the Skill**

Create a complete Markdown Skill with YAML frontmatter:

```yaml
---
name: memory-system-auto-instrumentation
description: Analyze an unfamiliar memory system and add request-correlated, non-overlapping performance instrumentation without changing business behavior.
---
```

The body must instruct the agent to:

```text
1. Discover public add/search routes, SDK calls, queue consumers, and async boundaries.
2. Trace each route to model, embedding, storage, vector, keyword, fusion, rerank, and format calls.
3. Reuse the target repository's logger, telemetry, configuration, and context propagation.
4. Publish a stage map before editing, with leaf/wrapper roles and canonical mappings.
5. Instrument real dependency calls, including an inner *.backend_call leaf when an async wrapper offloads blocking work.
6. Emit success/error JSONL events and re-raise the original exception.
7. Keep instrumentation default off and never log request or memory content.
8. Run focused tests and a short add/search probe.
9. Report covered, absent, missing, and unverified stages separately.
```

Include the required event fields:

```text
kind, ts, operation, stage, span_role, duration_ms, status, request_id
```

Require a monotonic clock for `duration_ms` and a timezone-aware wall clock for `ts`.

Include explicit rules for systems that lack optional stages: record the stage as `absent` in the coverage report and do not add a fake timing event.

- [ ] **Step 4: Run the focused test**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_instrumentation_skill_docs.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the Skill contract**

```bash
git add memory_bench_platform/skills/instrumentation/memory-system-auto-instrumentation/SKILL.md memory_bench_platform/tests/test_instrumentation_skill_docs.py
git commit -m "feat: add memory auto-instrumentation skill"
```

### Task 2: Add the meta-agent prompt and repository guidance

**Files:**
- Create: `memory_bench_platform/skills/instrumentation/memory-system-auto-instrumentation/prompts/meta-agent.md`
- Modify: `memory_bench_platform/tests/test_instrumentation_skill_docs.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: the Task 1 Skill at `memory_bench_platform/skills/instrumentation/memory-system-auto-instrumentation/SKILL.md`.
- Produces: a copyable prompt with inputs `TARGET_REPO`, `ADD_ENTRYPOINT`, `SEARCH_ENTRYPOINT`, `REQUEST_ID_CONTRACT`, `TRACE_ENABLEMENT`, `TRACE_OUTPUT`, and `TEST_COMMANDS`.

- [ ] **Step 1: Add failing tests for prompt ordering and README routing**

```python
PROMPT = SKILL.parent / "prompts/meta-agent.md"
README = Path(__file__).resolve().parents[2] / "README.md"


def test_meta_agent_prompt_requires_analysis_before_edits():
    text = PROMPT.read_text(encoding="utf-8")
    for marker in (
        "TARGET_REPO",
        "ADD_ENTRYPOINT",
        "SEARCH_ENTRYPOINT",
        "REQUEST_ID_CONTRACT",
        "TRACE_ENABLEMENT",
        "TRACE_OUTPUT",
        "TEST_COMMANDS",
    ):
        assert marker in text
    assert text.index("call graph") < text.index("edit source")
    assert "covered" in text
    assert "absent" in text
    assert "missing_request_id" in text
    assert "unverified" in text


def test_readme_distinguishes_guidance_and_runtime_skills():
    text = README.read_text(encoding="utf-8")
    assert "skills/instrumentation/" in text
    assert "指导型 Skill" in text
    assert "不由 Integration Skill loader 加载" in text
```

- [ ] **Step 2: Run the tests and confirm the prompt and README failures**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_instrumentation_skill_docs.py -v`

Expected: FAIL because `prompts/meta-agent.md` is missing and README lacks the guidance-only Skill text.

- [ ] **Step 3: Write the reusable prompt**

The prompt must open with this task and preserve the placeholders verbatim:

```text
请基于 TARGET_REPO 中的新记忆系统，按照随附的 memory-system-auto-instrumentation Skill，先分析 ADD_ENTRYPOINT 与 SEARCH_ENTRYPOINT 的真实调用结构，再设计并加入性能打点。
```

Require these gates:

```text
Before you edit source, return a call graph and stage map for review.
After approval, edit source with instrumentation default off.
Propagate request IDs according to REQUEST_ID_CONTRACT.
Write JSONL to TRACE_OUTPUT when TRACE_ENABLEMENT is enabled.
Run TEST_COMMANDS and a short add/search probe.
Return changed files, tests, covered stages, absent stages, missing_request_id paths, and unverified stages.
```

Add explicit prohibitions against content logging, silent exception conversion, invented stages, business refactors, and wrapper/leaf double counting.

- [ ] **Step 4: Add README routing text**

Add `skills/instrumentation/` to the Skill list and state that meta agents read these guidance-only Skills; the Integration Skill loader does not load them. Link to the new `SKILL.md` and prompt.

- [ ] **Step 5: Run focused documentation tests**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_instrumentation_skill_docs.py tests/test_docs_smoke.py -v`

Expected: PASS.

- [ ] **Step 6: Check prose and commit**

Run: `rg -n 'Here.s what|At its core' memory_bench_platform/skills/instrumentation README.md`

Expected: no matches.

```bash
git add README.md memory_bench_platform/skills/instrumentation/memory-system-auto-instrumentation/prompts/meta-agent.md memory_bench_platform/tests/test_instrumentation_skill_docs.py
git commit -m "docs: add meta-agent instrumentation prompt"
```

### Task 3: Verify the guidance-only Skill does not affect runtime discovery

**Files:**
- Modify: `memory_bench_platform/tests/test_instrumentation_skill_docs.py`

**Interfaces:**
- Consumes: `load_all_skills(skills_root: Path) -> dict[str, list]`.
- Produces: a regression test proving the new directory remains outside runtime Integration Skill discovery.

- [ ] **Step 1: Add the discovery-boundary test**

```python
from memory_bench_platform.loader import load_all_skills


def test_guidance_skill_is_not_loaded_as_runtime_integration():
    platform_root = Path(__file__).resolve().parents[1]
    loaded = load_all_skills(platform_root / "skills")
    loaded_ids = {
        item.id
        for kind in loaded.values()
        for item in kind
    }
    assert "memory-system-auto-instrumentation" not in loaded_ids
```

- [ ] **Step 2: Run the focused test**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest tests/test_instrumentation_skill_docs.py -v`

Expected: PASS. The test protects the intended non-runtime boundary rather than requiring an implementation change.

- [ ] **Step 3: Run platform and LoCoMo regression suites**

Run: `cd memory_bench_platform && /opt/homebrew/bin/python3.11 -m pytest -q`

Expected on macOS: every test outside the nine existing `/proc/stat` or `/proc/meminfo` failures passes.

Run: `cd locomo_test && /opt/homebrew/bin/python3.11 -m pytest -q`

Expected: `84 passed`.

- [ ] **Step 4: Commit the boundary test**

```bash
git add memory_bench_platform/tests/test_instrumentation_skill_docs.py
git commit -m "test: pin instrumentation skill discovery boundary"
```
