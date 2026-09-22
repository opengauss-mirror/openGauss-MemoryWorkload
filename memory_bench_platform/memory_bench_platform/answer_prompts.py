"""Dataset-owned answer rules and immutable prompt snapshots for scenario runs."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from string import Formatter

DEFAULT_RULES = (
    "Answer directly and concisely using the supplied memory evidence. "
    "Do not invent facts. If evidence is insufficient, say so."
)


def load_answer_prompt(skill_dir, manifest):
    config = manifest.answering
    if not config:
        return {"profile": "generic_memory_qa@1", "template": DEFAULT_RULES,
                "required_fields": []}
    profile = str(config.get("profile") or "").strip()
    relative = str(config.get("prompt_template") or "").strip()
    if not profile or not relative:
        raise ValueError("answering requires profile and prompt_template")
    root = Path(skill_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("answer prompt must be inside the benchmark skill")
    template = path.read_text(encoding="utf-8")
    fields = {name for _, name, _, _ in Formatter().parse(template) if name is not None}
    allowed = {"question", "question_date", "options"}
    if fields - allowed:
        raise ValueError(f"unsupported answer prompt fields: {sorted(fields - allowed)}")
    required = set(config.get("required_fields", [])) | fields
    if required - allowed:
        raise ValueError("unsupported required answer fields")
    return {"profile": profile, "template": template, "required_fields": sorted(required)}


def render_answer_prompt(config, question):
    config = config or {"template": DEFAULT_RULES, "required_fields": []}
    values = {"question": question.question,
              "question_date": question.metadata.get("question_date"),
              "options": question.metadata.get("options")}
    for field in config["required_fields"]:
        if values.get(field) is None or values[field] == "" or values[field] == []:
            raise ValueError(f"question {question.question_id}: missing answer field {field}")
    values = {key: "\n".join(map(str, value)) if isinstance(value, list)
              else str(value or "") for key, value in values.items()}
    return config["template"].format(**values)


def snapshot_prompts(run_dir, config, skill_dir, manifest, integration):
    run_dir, skill_dir = Path(run_dir), Path(skill_dir)
    target = run_dir / "prompts"
    target.mkdir(parents=True, exist_ok=True)
    records = {}
    texts = {"answer": (config["profile"], config["template"])}
    judge_path = manifest.judging.get("prompt_template")
    if judge_path:
        texts["judge"] = (manifest.judging.get("profile"),
                          (skill_dir / judge_path).read_text(encoding="utf-8"))
    for name, (profile, text) in texts.items():
        (target / f"{name}.txt").write_text(text, encoding="utf-8")
        records[name] = {"profile": profile, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                         "snapshot": f"prompts/{name}.txt"}
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=skill_dir, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=skill_dir, text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    judge = manifest.judging
    judge_model_env = judge.get("env", {}).get("model", "")
    # The Agent owns its model configuration. Record only an explicit model label;
    # never copy its config or arbitrary environment (which may contain credentials).
    models = {
        "answer": {"model": os.environ.get("MEMORY_BENCH_ANSWER_MODEL") or None,
                   "source": "operator-declared; verify against Agent response artifacts",
                   "parameters": "Agent-owned; not overridden by the prompt profile"},
        "judge": {"model": os.environ.get(judge_model_env) or judge.get("model"),
                  "temperature": 0 if judge.get("api_format", "openai") != "anthropic" else None,
                  "max_tokens": int(judge.get("max_tokens") or os.environ.get("MEMORY_BENCH_JUDGE_MAX_TOKENS", "256"))},
    }
    record = {"models": models, "benchmark": manifest.id, "integration": integration, "prompts": records,
              "git_commit": commit, "working_tree_dirty": dirty,
              "runtime_records": "records/", "model_requests": "artifacts/",
              "note": "Backend extraction prompts are owned by the backend; not overridden by this profile."}
    (run_dir / "prompt_manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def finalize_prompt_models(run_dir):
    """Record observed OpenClaw model identity without copying credentials."""
    root = Path(run_dir)
    path = root / "prompt_manifest.json"
    if not path.exists():
        return
    record = json.loads(path.read_text(encoding="utf-8"))
    observed = set()
    for artifact in (root / "artifacts" / "step-stdout").glob("*-agent-answer.json"):
        try:
            result = json.loads(artifact.read_text(encoding="utf-8"))
            raw = result.get("raw", {})
            payload = raw.get("result", raw)
            meta = payload.get("meta", {}).get("agentMeta", {})
            if not meta.get("model") and payload.get("model"):
                meta = {"model": payload["model"], "provider": payload.get("provider", "")}
            if meta.get("model"):
                observed.add((str(meta.get("provider") or ""), str(meta["model"])))
        except (ValueError, AttributeError):
            continue
    record.setdefault("models", {}).setdefault("answer", {})["observed"] = [
        {"provider": provider, "model": model} for provider, model in sorted(observed)
    ]
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
