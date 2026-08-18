import json
import subprocess
import sys
from pathlib import Path


def test_generic_cli_runner_echoes_user_message():
    script = Path("skills/agents/generic-cli/scripts/run_task.py")
    payload = {
        "task_id": "step-1",
        "messages": [{"role": "user", "content": "What is your name?"}],
        "metadata": {},
    }
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    response = json.loads(proc.stdout)
    assert response["status"] == "ok"
    assert response["turns"][0]["text"] == "What is your name?"
