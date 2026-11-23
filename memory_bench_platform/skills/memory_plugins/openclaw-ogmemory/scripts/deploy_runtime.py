"""deployment/1 bridge to the existing native oGMemory lifecycle adapter."""
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent
COMMON = runpy.run_path(str(ROOT.parent.parent / "scripts/deployment_openclaw.py"))
MUTABLE = ("accountId", "userId", "agentId", "authAccountId", "autoCapture", "autoRecall")
PLUGIN = "og-memory-context-engine"


def digest(directory):
    return COMMON["fingerprint"](directory, PLUGIN, MUTABLE, [directory / "plugin"])


def prepare(spec, context):
    if spec.get("options"):
        raise ValueError("oGMemory deployment does not accept extra options")
    adapter = runpy.run_path(str(ROOT / "run_lifecycle.py"))
    directory = Path(context["directory"])
    if not spec.get("upstream_plugin"):
        raise ValueError("oGMemory deployment requires upstream_plugin")
    adapter["prepare_runtime"](Path(spec["source_config"]), Path(spec["upstream_plugin"]),
        directory, context["memory_url"], runtime_path="/runtime", gateway_port=context["gateway_port"])
    path = directory / "openclaw.json"
    config = json.loads(path.read_text())
    config["gateway"]["bind"] = "lan"
    config["plugins"]["entries"][PLUGIN]["hooks"] = {"allowConversationAccess": True, "allowPromptInjection": True}
    adapter["save"](path, config)
    return {"service": COMMON["service"](directory), "metadata": {"fingerprint": digest(directory)}}


def environment(binding, endpoints):
    directory = Path(binding["directory"])
    config = json.loads((directory / "openclaw.json").read_text())
    return {**COMMON["environment"](directory, endpoints),
            "OGMEM_PLUGIN_STATE_FILE": str(directory / "phase.json"),
            "OGMEM_GATEWAY_CONTAINER": endpoints["container"], "OGMEM_API_URL": endpoints["memory_url"],
            "OGMEM_API_KEY": config["plugins"]["entries"][PLUGIN]["config"].get("authApiKey", "")}


def issues(binding):
    directory = Path(binding["directory"])
    result = []
    if digest(directory) != binding["metadata"]["fingerprint"]:
        result.append("plugin runtime configuration or code changed")
    phase = json.loads((directory / "phase.json").read_text())
    if phase.get("phase") and (phase["phase"] != "inactive" or not phase.get("gateway_ready")):
        result.append("native Gateway has not confirmed inactive phase")
    if phase.get("active"):
        result.append("plugin runtime has unfinished lifecycle state")
    config = json.loads((directory / "openclaw.json").read_text())["plugins"]["entries"][PLUGIN]["config"]
    if config.get("autoCapture") or config.get("autoRecall"):
        result.append("plugin runtime did not restore inactive capture/recall")
    return result
