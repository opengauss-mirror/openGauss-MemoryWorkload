# Compose Deploy and Native Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `memory-bench deploy`, `deploy status`, `undeploy`, and `run --deployment` for isolated or explicitly shared Compose topology and native workflow execution.

**Architecture:** Parse one strict deployment YAML into physical project groups, render Compose per group, and control Docker through one injectable CLI adapter. Persist an owned-resource manifest atomically; native run resolves that manifest and invokes the existing run path once per selected combination. The follow-on pressure-test plan adds multi-user execution to the same manifest and CLI entry point.

**Tech Stack:** Python >=3.11, Pydantic >=2.7, PyYAML >=6.0.1, Docker Compose v2 CLI, pytest >=8.2.

**Spec:** `docs/superpowers/specs/2026-10-09-compose-deployment-design.md`

## Global Constraints

- Keep existing `memory-bench run` without `--deployment` behavior unchanged.
- `deploy --agent-config` and `--memory-config` accept one `<instance ID>:<parameter name>=<JSON value>` per occurrence; only allowlisted JSON/YAML configuration fields are changed in per-instance copies, and the source files remain unchanged.
- Each standalone combination has its own Compose project; combinations sharing `memory_instance` have one project, one memory/database set, and separate Agent containers and networks.
- Accept image names and local tar archives; do not add archive SHA256 or architecture checks.
- `seed.enabled` and `warmup.enabled` default to false; enabling deploy seed requires readiness; user-scoped seed belongs to the follow-on pressure plan.
- Generated Compose and deployment state must not contain secret values; all host-published ports bind to loopback.
- No product code is to be written before this plan is reviewed and an execution method is chosen.

## Review Focus

- Two combinations reference the same `memory_instance`: generate one memory container and count its resource limit once (Task 2).
- An override names a missing or wrong-role instance, an unknown/duplicate parameter, or a value of the wrong type: fail before any Docker call (Task 1).
- A deploy fails after resource boosting: restore limits and remove only this deploy's new projects and volumes (Task 4).
- A container is replaced after deploy: `deploy status` and `run --deployment` report drift and refuse use (Tasks 4–5).
- A deployed run inherits a conflicting parent endpoint: the combination's discovered endpoint wins only in its child process (Task 5).

---

### Task 1: Strict deployment configuration and ownership resolution

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/deployment_config.py`
- Create: `memory_bench_platform/tests/test_deployment_config.py`

**Interfaces:**
- Produces: `load_deployment(path: Path) -> DeploymentConfig`, `apply_config_overrides(config: DeploymentConfig, agent_items: list[str], memory_items: list[str]) -> DeploymentConfig`, and `resolve_groups(config: DeploymentConfig) -> tuple[DeploymentGroup, ...]`.
- `DeploymentConfig` exposes resolved `agent_config_overrides: dict[str, dict[str, object]]` and `memory_config_overrides: dict[str, dict[str, object]]`, keyed by combination ID and physical memory instance ID; `apply_config_overrides` validates values against template `config_files.*.parameters` and returns a copy.
- `DeploymentGroup` carries `project_key`, `combination_ids`, `shared_memory_instance_id | None`, and concrete service templates; later tasks use it without reparsing YAML.

- [ ] **Step 1: Write failing parsing tests.** Test `test_shared_memory_group_is_resolved_once` asserts two combinations referencing `mem0-common` produce one group with one memory template and two Agent entries. Test `test_missing_mock_reference_fails_before_docker` rejects an absent owner Mock. Test typed configuration overrides for Agent combination and independent/shared memory IDs; reject unknown/wrong-role instances, unknown/duplicate parameters, malformed JSON, wrong types, missing JSON Pointer targets, overlapping mounts, and unsupported formats before Docker. Also cover unknown YAML fields, duplicate IDs, `memory` xor `memory_instance`, missing image/archive, paths relative to YAML, preparation enabled/command/readiness rules, graph/vector references, and rejection of external benchmark entrypoints.

  ```python
  groups = resolve_groups(load_deployment(config_path))
  assert len(groups) == 1
  assert groups[0].combination_ids == ("claw-common", "worker-common")
  changed = apply_config_overrides(config, ['claw-common:log_level="debug"'], ['mem0-common:workers=4'])
  assert changed.agent_config_overrides["claw-common"]["log_level"] == "debug"
  assert changed.memory_config_overrides["mem0-common"]["workers"] == 4
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_config.py -q`; expect import or assertion failures.
- [ ] **Step 3: Implement `load_deployment`, `apply_config_overrides`, and `resolve_groups`.** Use strict Pydantic models and `yaml.safe_load`; validate Skill IDs and benchmark entrypoint through existing loader/integration functions. Parse `<ID>:<parameter>=<JSON value>` using the first `=` only; assign Agent overrides by combination ID and memory overrides by physical instance ID. Resolve shared instance ownership and reject unreferenced or conflicting service endpoints. Parse `pressure_tests` as configuration data but defer its Agent capability checks to the second plan; until then, the deploy CLI explicitly rejects nonempty `pressure_tests` rather than silently ignoring them.
- [ ] **Step 4: Verify green.** Run the focused test module; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit the new model and focused tests.

### Task 2: Deterministic Compose rendering and preflight capacity

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/deployment_compose.py`
- Create: `memory_bench_platform/memory_bench_platform/deployment_config_files.py`
- Create: `memory_bench_platform/tests/test_deployment_compose.py`
- Create: `memory_bench_platform/tests/test_deployment_config_files.py`

**Interfaces:**
- Consumes: `DeploymentGroup` from Task 1.
- Produces: `prepare_instance_configs(group: DeploymentGroup, root: Path) -> dict[str, list[dict[str, object]]]`, `render_project(group: DeploymentGroup, deployment_name: str, generated_mounts: dict[str, list[dict[str, object]]]) -> dict[str, object]` and `required_resources(groups: tuple[DeploymentGroup, ...]) -> ResourceRequest`, where `ResourceRequest` exposes aggregate `cpus` and `memory_mb`.

- [ ] **Step 1: Write failing renderer tests.** Assert standalone groups have isolated project names, networks, volumes, health dependencies and loopback published ports. Assert shared groups have one memory/database/Mock set, separate `agent_net_<combination>` networks, and per-Agent state. Assert unchanged source JSON/YAML, per-instance 0600 config copies in a 0700 directory, typed field changes, one copy per shared memory instance, and read-only mounts at declared targets, ordinary `config_mounts` stay unchanged, and `command` stays as declared. With one 1024 MB memory and two 256 MB Agents, assert `required_resources` reports 1536 MB; unused optional graph/Mock resources are absent.

  ```python
  mounts = prepare_instance_configs(shared_group, tmp_path)
  compose = render_project(shared_group, "suite", mounts)
  assert sum(name.startswith("memory") for name in compose["services"]) == 1
  assert compose["services"]["agent_claw_common"]["volumes"][0]["read_only"] is True
  assert required_resources((shared_group,)).memory_mb == 1536
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_compose.py memory_bench_platform/tests/test_deployment_config_files.py -q`; expect failures.
- [ ] **Step 3: Implement config copies and the renderer.** Resolve RFC 6901 pointers without creating missing keys, preserve source values outside overrides, and serialize JSON/YAML safely. Generate Compose data only from resolved groups and generated mounts; render host secrets as environment references supplied at Compose invocation, never literal values in files. Keep service names deterministic and validate generated dependency/network references before writing YAML.
- [ ] **Step 4: Verify green.** Run the focused test module; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit the renderer and tests.

### Task 3: Docker command seam and durable deployment state

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/deployment_docker.py`
- Create: `memory_bench_platform/memory_bench_platform/deployment_store.py`
- Create: `memory_bench_platform/tests/test_deployment_docker.py`
- Create: `memory_bench_platform/tests/test_deployment_store.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `DockerClient(command: Callable[..., CompletedProcess])` with `load_image`, `info`, `compose_up`, `compose_down`, `inspect`, `exec_json`, and `update_resources` methods; `DeploymentStore(root: Path)` with `create`, `read`, `write_atomic`, `acquire_lock`, and `release_lock`.
- Defines: `DeploymentState` as the typed persisted manifest consumed by Tasks 4–5, and `DeploymentLocked` for lock contention.
- Later tasks pass a recording command function to `DockerClient`; production uses subprocess with argv, timeouts, and no shell.

- [ ] **Step 1: Write failing adapter/store tests.** Assert `docker load` verifies the requested name exists afterward, Compose calls use the recorded project/file, `exec_json` rejects invalid JSON, state writes replace atomically, a second lock holder is rejected, and no serialized state contains an environment secret or CLI override value.

  ```python
  state = store.read("suite")
  assert "PRIVATE_TOKEN" not in state.model_dump_json()
  with pytest.raises(DeploymentLocked):
      store.acquire_lock("suite")
  ```
- [ ] **Step 2: Verify red.** Run both focused test modules; expect failures.
- [ ] **Step 3: Implement the seam and store.** State records project/container/image IDs, selected ports, config fingerprint, resource limits, seed/readiness outcome, and ownership of volumes/directories. Use atomic file replacement and an OS-backed lock; avoid deleting unrecorded Docker resources. Ignore `.memory-bench/deployments/` in Git.
- [ ] **Step 4: Verify green.** Run both focused test modules; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit the Docker adapter, store, and tests.

### Task 4: Deploy, status, rollback, and undeploy lifecycle

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/deployment_lifecycle.py`
- Create: `memory_bench_platform/tests/test_deployment_lifecycle.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `deploy(config_path: Path, cwd: Path, docker: DockerClient, agent_items: list[str] | None = None, memory_items: list[str] | None = None) -> DeploymentState`, `status(name: str, cwd: Path, docker: DockerClient) -> DeploymentStatus`, `undeploy(name: str, cwd: Path, docker: DockerClient) -> None`; defines `DeploymentStatus` and `DeploymentFailed`.

- [ ] **Step 1: Write failing lifecycle tests.** A recording Docker fake checks preflight capacity/NUMA before any `compose_up` (including rejection of NUMA on non-Linux); malformed or missing config fields fail before startup; health/version/effective endpoint/dependency checks before ready; `verify.command` reports every effective overridden value, and a mismatch or later file/mount drift is detected; once-only shared seed; readiness timeout and consecutive-success count; temporary vector resource boost and restoration on success, seed error, and interruption; rollback of only new projects on partial failure; drift on changed container/image/config; safe retry of failed undeploy. Include the Review Focus failure-after-boost and replaced-container cases.

  ```python
  with pytest.raises(DeploymentFailed):
      deploy(config_path, tmp_path, recording_docker)
  assert recording_docker.resources_restored("vector")
  assert recording_docker.removed_projects == {"suite-owned-project"}
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_lifecycle.py -q`; expect failures.
- [ ] **Step 3: Implement lifecycle functions.** Apply and validate config overrides and generate instance files before Docker; load archives and render projects, run preflight, start and inspect services, run read-only verify commands and compare actual effective parameter values, then optional seed/readiness with a `finally` restoration path. Include source/generated file hashes and parameter origins, but no values, in state; keep expected values in protected instance files. Persist ready only after every group succeeds. `status` reads live Docker state and verifies file hashes, mounts, and effective values; `undeploy` removes only owned projects/volumes/config copies and keeps state if cleanup fails.
- [ ] **Step 4: Verify green.** Run the focused test module; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit lifecycle code and tests.

### Task 5: Bind deployed combinations to the existing native runner

**Files:**
- Create: `memory_bench_platform/memory_bench_platform/deployment_run.py`
- Create: `memory_bench_platform/tests/test_deployment_run.py`
- Modify: `memory_bench_platform/memory_bench_platform/cli.py`
- Modify: `memory_bench_platform/tests/test_cli_smoke.py`

**Interfaces:**
- Consumes: `status` and `DeploymentStore` from Tasks 3–4.
- Produces: `run_deployment(name: str, cwd: Path, combination_id: str | None, docker: DockerClient, invoke: Callable[..., int]) -> int` and CLI routes `deploy --config [--agent-config ID:PARAM=JSON] [--memory-config ID:PARAM=JSON]`, `deploy status --deployment`, `undeploy --deployment`, `run --deployment [--combination]`.

- [ ] **Step 1: Write failing CLI and child-routing tests.** Assert `deploy` accepts repeated Agent/memory config options, passes parsed lists to `apply_config_overrides`, and fails unknown/duplicate selectors before container startup. Assert existing manual run still requires `--benchmark`/`--agent`; deployment run does not; conflicting manual selection flags fail; each child gets only its combination's Agent/Memory URLs and explicitly referenced credentials; inherited conflicting URL cannot win; drift prevents invocation; enabled warmup runs before each workflow and stays outside measured metrics; a shared-memory sequential suite records order and physical instance ID; one failed combination does not skip later combinations; interrupt returns 130 with partial summary.

  ```python
  rc = run_deployment("suite", tmp_path, None, docker, record_child)
  assert rc == 0
  assert child_envs[0]["MEM0_API_URL"] == discovered_urls["claw-mem0"]
  assert "GRAPHMEM_API_URL" not in child_envs[0]
  ```
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_run.py memory_bench_platform/tests/test_cli_smoke.py -q`; expect failures.
- [ ] **Step 3: Implement CLI dispatch and `run_deployment`.** Dispatch deployment commands before manual `_execution_options`; generate each native child argv from the resolved combination; hold the deployment resource lock, validate live status, run optional warmup, and write a suite summary referencing normal `runs/<run-id>/` outputs. Reject `--pressure-test` explicitly until the second plan implements it.
- [ ] **Step 4: Verify green and regressions.** Run the two focused modules plus `test_run_with_builder.py` and `test_multi_user_replay.py`; expect all tests to pass.
- [ ] **Step 5: Commit.** Commit CLI/run binding and tests.

### Task 6: User documentation and deployment smoke gate

**Files:**
- Modify: `README.md`
- Create: `docs/deployment-yaml.md`
- Create: `memory_bench_platform/tests/test_deployment_cli_integration.py`

**Interfaces:**
- Consumes: Tasks 1–5; no new public Python interface.

- [ ] **Step 1: Write a failing command integration test.** With a Docker CLI recorder and small fake Skill fixtures, assert `deploy --agent-config 'claw-mem0:log_level="debug"' → status → run --combination → undeploy` issues commands in order, records the generated config hash and CLI parameter origin, detects effective-value or mount drift, keeps CLI values and secrets out of state/logs, and removes only recorded resources.

  ```python
  assert cli("deploy", "--config", config_path,
             "--agent-config", 'claw-mem0:log_level="debug"') == 0
  assert cli("run", "--deployment", "suite", "--combination", "claw-mem0") == 0
  assert set(docker.removed_projects) == set(deployment_state.owned_projects)
  ```
- [ ] **Step 2: Verify red.** Run the focused integration test; expect failure.
- [ ] **Step 3: Complete docs and integration wiring.** Document YAML, image archive use, optional graph/Mock, shared instance constraints, baseline seed/readiness/warmup switches, `--agent-config`/`--memory-config` selectors and typed field overrides, generated-file permissions, status/drift, cleanup, and the fact that pressure execution follows the second plan.
- [ ] **Step 4: Verify green.** Run focused deployment tests and `git diff --check`; expect all tests and formatting checks to pass. Run a real Compose smoke only when suitable images/Docker are available, and report skipped otherwise.
- [ ] **Step 5: Commit.** Commit docs and integration test.

## Completion Gate

Run all deployment-focused tests and the native-run regressions. Confirm a real Docker end-to-end claim only if actual images and datasets were used. The second plan must pass before declaring the full spec implemented.
