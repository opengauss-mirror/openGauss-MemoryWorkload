from pathlib import Path

from memory_bench_platform.loader import load_all_skills


def test_load_all_skills_reads_benchmark_and_agent_manifests(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "benchmarks" / "locomo").mkdir(parents=True)
    (skills / "agents" / "generic-cli").mkdir(parents=True)
    (skills / "benchmarks" / "locomo" / "manifest.yaml").write_text(
        "kind: benchmark\nid: locomo\nversion: 0.1.0\nentry:\n  case_builder: scripts/build_tasks.py\n",
        encoding="utf-8",
    )
    (skills / "agents" / "generic-cli" / "manifest.yaml").write_text(
        "kind: agent\nid: generic-cli\nversion: 0.1.0\nentry:\n  runner: scripts/run_task.py\n",
        encoding="utf-8",
    )
    loaded = load_all_skills(skills)
    assert loaded["benchmarks"][0].id == "locomo"
    assert loaded["benchmarks"][0].entry.case_builder == "scripts/build_tasks.py"
    assert loaded["agents"][0].id == "generic-cli"
