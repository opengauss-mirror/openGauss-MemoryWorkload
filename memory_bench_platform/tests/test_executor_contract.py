from memory_bench_platform.executor import SkillCommand


def test_skill_command_renders_as_process_args():
    cmd = SkillCommand(script="scripts/run_task.py", args=["--task", "task-1"])
    assert cmd.to_argv() == ["scripts/run_task.py", "--task", "task-1"]
