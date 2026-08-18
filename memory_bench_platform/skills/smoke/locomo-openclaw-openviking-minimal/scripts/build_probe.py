from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    script_path = Path(__file__).resolve()
    skill_root = script_path.parent.parent
    project_root = script_path.parents[4]
    repo_root = project_root.parent
    run_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None

    payload = {
        "smoke_id": "locomo-openclaw-openviking-minimal",
        "repo_root": str(repo_root),
        "project_root": str(project_root),
        "skill_root": str(skill_root),
        "mini_test_config": str(repo_root / "locomo_test" / "configs" / "mini-test.toml"),
        "smoke_test_config": str(repo_root / "locomo_test" / "configs" / "smoke-test.toml"),
        "env_toml": str(repo_root / "locomo_test" / "configs" / "env.toml"),
        "remote_entrypoint": str(repo_root / "tools" / "test_entrypoints" / "run_locomo_test_remote.sh"),
        "expected_artifacts": [
            "pipeline.log",
            ".ingest_record.json",
            "qa_results.csv",
            "qa_diagnostics.json",
            "qa_reindex.json",
        ],
        "stages": [
            "session_bootstrap",
            "message_ingest",
            "session_commit",
            "memory_extraction",
            "reindex_or_consistency",
            "recall_probe",
            "answer_probe",
            "result_parse",
        ],
        "run_dir": str(run_dir) if run_dir else "",
        "smoke_run_name": "locomo-openclaw-openviking-minimal-smoke",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
