# OpenClaw Gateway Pool Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one deployed combination run through an optional Gateway Pool in front of multiple isolated OpenClaw Gateway containers.

**Architecture:** Extend the existing strict deployment YAML and physical group model with one optional Pool template per combination and a replica count. Render named Gateway services and explicit Pool upstreams inside the existing Compose project; bind native runs to the Pool URL and provide a stable per-user routing identity. Keep the current single Agent path when no Pool is configured.

**Tech Stack:** Python 3.11+, Pydantic 2, PyYAML, Docker Compose v2, pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-compose-deployment-design.md`, section “OpenClaw Gateway Pool 扩展”.

## Global Constraints

- Keep `schema: deployment/1` and the behavior of combinations without `gateway_pool` unchanged.
- Pool templates use `kind: openclaw_http_pool_v1` and the reference `gateway_pool.py` argv contract: `--host`, `--port`, repeated `--upstream`, `--expected`.
- One Pool and its Gateway replicas belong to one combination; even a shared memory project must not share their routes or state with another combination.
- Agent replicas each have an independent config copy and state mount; `--agent-config <combination>:...` applies the same permitted override to every replica.
- Publish only the Pool client port for pooled runs; any individually published Agent ports bind to `127.0.0.1`.
- A Pool request requires a stable `user` value. Preserve the reference Pool's `user` and `X-OpenClaw-Memory-User` equality check; do not treat round robin routing as user isolation.
- Existing `preparation` commands execute inside a container. Reject Agent-targeted seed or warmup on a pooled combination because those commands bypass the Pool; user-level preparation is part of the later pressure-test implementation.
- The existing deploy command rejects `pressure_tests`; this plan does not implement `run --pressure-test` or general Agent autoscaling.

## Review Focus

- An unknown Pool template, non-OpenClaw Agent, zero replica count, or replicas without a Pool must fail before any Docker call (Task 1).
- Shared-memory combinations must get separate Pools and Gateway state directories despite sharing one Compose project (Tasks 1–2).
- A Pool reporting fewer or different upstreams than the rendered set must never produce ready status (Task 3).
- A pooled request with no route identity must fail locally; body `user` and memory-user header must always match (Task 4).
- A replaced or restarted Pool or Gateway must fail pre-run drift checks; a mid-run Pool restart must fail the combination instead of silently remapping users (Tasks 3–4).

---

### Task 1: Parse and resolve Pool topology

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/deployment_config.py`
- Test: `memory_bench_platform/tests/test_deployment_config.py`

**Interfaces:**
- Add `GatewayPool(Service)` with `kind: Literal["openclaw_http_pool_v1"]`, required nonempty command prefix, container_port and healthcheck; reject Agent/Memory-only `skill`, `client`, `verify`, `config_files` and `service_endpoints` fields on Pool templates.
- Add `DeploymentConfig.gateway_pools: dict[str, GatewayPool]` and `Combination.gateway_pool: str | None`, `Combination.agent_replicas: int = 1`.
- Add `DeploymentGroup.gateway_pools: dict[str, GatewayPool]`, keyed by combination ID; `Combination.agent_replicas` remains the authoritative count.
- `load_deployment(path)` performs all reference, transport, count, and Agent-target preparation validation before Docker preflight; `resolve_groups(config)` carries the selected templates into each group.

- [ ] **Step 1: Write failing tests.** `test_gateway_pool_resolves_per_combination` creates two OpenClaw combinations sharing one memory instance, selects one Pool template, and asserts distinct group Pool entries and replica counts. `test_gateway_pool_rejects_invalid_topology_before_docker` covers unknown template, non-OpenClaw Skill, missing/nonpositive count, count > 1 without Pool, unsupported kind, absent command/port, and Agent-targeted seed/warmup.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_config.py -q`; new tests must fail for the missing contract.
- [ ] **Step 3: Implement the models and validation.** Reuse existing ID, image/archive, mount, healthcheck and resource checks; require the paired OpenClaw Agent `client.endpoint_env` to be `OPENCLAW_GATEWAY_URL`, reject a conflicting `OPENCLAW_TRANSPORT=cli` template setting, and force HTTP transport at run time. Keep non-Pool configurations parsing exactly as before.
- [ ] **Step 4: Verify green.** Run the same test module; all tests pass.
- [ ] **Step 5: Commit.** Commit only the config module and its tests.

### Task 2: Render isolated replicas and explicit Pool upstreams

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/deployment_config_files.py`
- Modify: `memory_bench_platform/memory_bench_platform/deployment_compose.py`
- Test: `memory_bench_platform/tests/test_deployment_compose.py`
- Test: `memory_bench_platform/tests/test_deployment_config_files.py`

**Interfaces:**
- `prepare_instance_configs(group, root)` returns config mounts for `agent_<id>_1` … `agent_<id>_N` on pooled combinations; keep `agent_<id>` for non-Pool combinations.
- `render_project(group, deployment_name, generated_mounts, deployment_root)` renders each named Agent replica and `gateway_pool_<id>`, with explicit `--upstream http://agent_<id>_<n>:<port>` args and healthy dependencies.
- `required_resources(groups)` multiplies each pooled Agent's limits by its replica count and adds Pool limits once.

- [ ] **Step 1: Write failing tests.** Assert three Gateways have separate config paths, separate state volumes, identical approved override values, one Pool, exact ordered upstream args, Agent network isolation across combinations, and Pool-only client port publishing. Assert shared memory and its vector/graph resources are counted once, while Agent and Pool resources are counted per physical container.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_compose.py memory_bench_platform/tests/test_deployment_config_files.py -q` and observe the new failures.
- [ ] **Step 3: Implement replica naming, copies and rendering.** Reuse `_service` for ordinary containers. Construct Pool command from the validated prefix and generated names; its health dependency names must match rendered Agent services. Do not use Compose `--scale` or DNS discovery.
- [ ] **Step 4: Verify green.** Run both test modules; all tests pass.
- [ ] **Step 5: Commit.** Commit renderer, config-copy changes, and tests.

### Task 3: Track Pool health and deployment drift

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/deployment_lifecycle.py`
- Modify: `memory_bench_platform/memory_bench_platform/deployment_store.py` only if the current state model cannot retain Pool restart evidence
- Test: `memory_bench_platform/tests/test_deployment_lifecycle.py`

**Interfaces:**
- `_service_templates(group, rendered)` enumerates every physical replica and Pool, so the existing deploy/status state maps include their IDs, images, ports and config hashes.
- Add `_pool_status(url: str) -> dict` as a bounded HTTP JSON probe of `/pool/status`; deploy and status compare its `upstreams` set to the rendered explicit upstream URLs.
- Add `DeploymentState.restart_counts: dict[str, int]` with an empty default for old manifests. Record Docker restart count for each Pool and Gateway at deploy; status compares it with the current inspect result, alongside existing container/image/health checks.

- [ ] **Step 1: Write failing tests.** Recording Docker and monkeypatched `_pool_status` prove all replica verify commands execute, Pool starts only after its dependencies, wrong/missing upstream fails deploy with owned-project rollback, changed upstream fails status, and replacement/restart of any Pool or replica yields drift. Cover a Pool image tar load and resource preflight before `compose_up`.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_lifecycle.py -q`; new tests fail.
- [ ] **Step 3: Implement lifecycle integration.** Include Pool in image/archive and environment checks; inspect each physical service. Probe only the host-loopback Pool port recorded from Docker. Preserve existing rollback and undeploy ownership behavior.
- [ ] **Step 4: Verify green.** Run the focused lifecycle tests; all pass.
- [ ] **Step 5: Commit.** Commit lifecycle/state changes and tests.

### Task 4: Bind native runs to the Pool with sticky user routing

**Files:**
- Modify: `memory_bench_platform/memory_bench_platform/deployment_run.py`
- Modify: `memory_bench_platform/skills/agents/openclaw/scripts/run_task.py`
- Test: `memory_bench_platform/tests/test_deployment_run.py`
- Test: `memory_bench_platform/tests/test_openclaw_runner.py`

**Interfaces:**
- `run_deployment(...)` sets the Agent endpoint to `gateway_pool_<id>` for pooled combinations, `OPENCLAW_POOL_ENABLED=1`, and `OPENCLAW_POOL_ROUTE_PREFIX` to a stable, run-local `<suite_id>:<combination_id>`; non-Pool runs retain their current endpoint and environment behavior.
- `build_openclaw_http_request(request)` derives the pooled route user from `OPENCLAW_POOL_ROUTE_PREFIX` plus `metadata.user_id` or a stable `default` label; sets body `user` and matching `X-OpenClaw-Memory-User`. If `OPENCLAW_POOL_ENABLED=1` and the prefix is absent, raise before sending HTTP.
- `run_deployment` checks the Pool and all of its Gateway containers' restart evidence again after native invocation and marks that combination failed if any changed during the run. Query `/pool/status` for a redacted user-to-upstream mapping and per-upstream request counts in the suite summary; never archive raw user identities.

- [ ] **Step 1: Write failing tests.** Assert pooled run receives Pool URL, HTTP transport and route prefix while a normal run still receives direct Agent URL. Assert two requests for one user use the same `user`/header, another user gets a different value, and no prefix yields a local error in pooled mode. Assert Pool or Gateway restart during the injected native invocation fails the combination. Retain existing OpenClaw HTTP tests unchanged outside pool mode.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_run.py memory_bench_platform/tests/test_openclaw_runner.py -q`; new tests fail.
- [ ] **Step 3: Implement run routing.** Clear inherited Pool-specific endpoint/identity environment variables before setting this combination's values; use the stored host port. Reuse the current per-combination child process and suite summary. Keep the HTTP request JSON shape unchanged when no Pool is active, and ensure Pool status output is redacted before archiving.
- [ ] **Step 4: Verify green.** Run both focused modules; all pass.
- [ ] **Step 5: Commit.** Commit run binding, OpenClaw adapter, and tests.

### Task 5: User-facing contract and final verification

**Files:**
- Modify: `docs/deployment-yaml.md`
- Modify: `memory_bench_platform/tests/test_deployment_cli_integration.py`

**Interfaces:**
- Document exact YAML fields, reference-compatible Pool image/command requirement, `--agent-config` fan-out, preparation restriction, deploy/status/run commands, and the present `pressure_tests` limitation.

- [ ] **Step 1: Write failing CLI integration coverage.** Configure a pooled combination, deploy with a recording Docker fake, run it, inspect the child process environment and suite summary, then undeploy. Include a no-Pool control path.
- [ ] **Step 2: Verify red.** Run `python -m pytest memory_bench_platform/tests/test_deployment_cli_integration.py -q`; new pooled case fails.
- [ ] **Step 3: Complete documentation and integration wiring.** Add a complete example using a Pool image that contains `gateway_pool.py`; preserve the current command syntax and all unpooled documentation.
- [ ] **Step 4: Verify green.** Run `python -m pytest memory_bench_platform/tests/test_deployment_config.py memory_bench_platform/tests/test_deployment_config_files.py memory_bench_platform/tests/test_deployment_compose.py memory_bench_platform/tests/test_deployment_lifecycle.py memory_bench_platform/tests/test_deployment_run.py memory_bench_platform/tests/test_deployment_cli_integration.py memory_bench_platform/tests/test_openclaw_runner.py -q` and `git diff --check`; both pass. If a real compatible image and Docker daemon are available, run a two-Gateway smoke test and verify `/pool/status` and same-user upstream affinity; otherwise report that real-container validation was not run.
- [ ] **Step 5: Commit.** Commit documentation and integration test changes.

After these tasks, request a whole-branch review, address concrete findings, and report test evidence and any real Docker limitation. Do not claim the separate multi-user pressure-test feature is implemented by this plan.
