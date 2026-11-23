"""Deploy the official Mem0 plugin using its existing OSS runtime preparation."""
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent
COMMON = runpy.run_path(str(ROOT.parent.parent / "scripts/deployment_openclaw.py"))
MEMORY_PLACEMENT = "agent"
PLUGIN = "openclaw-mem0"
MUTABLE = ("autoCapture", "autoRecall", "userId")


def digest(directory):
    provenance = json.loads((directory / "provenance.json").read_text())
    return COMMON["fingerprint"](directory, PLUGIN, MUTABLE,
        [Path(provenance["upstream"]) / name for name in provenance["files"]])


def prepare(spec, context):
    if context.get("memory_port") is not None:
        raise ValueError("Official Mem0 OSS plugin is an embedded SDK; omit memory container_port")
    adapter = runpy.run_path(str(ROOT / "run_lifecycle.py"))
    directory = Path(context["directory"])
    if not spec.get("upstream_plugin"):
        raise ValueError("Mem0 deployment requires the installed official npm package")
    upstream = Path(spec["upstream_plugin"]).resolve()
    # Preserve sibling npm dependencies when mounting the scoped package.
    package_root = upstream.parents[1]
    if package_root.name != "node_modules":
        raise ValueError("Use node_modules/@mem0/openclaw-mem0, with its installed dependencies")
    relative_package = upstream.relative_to(package_root)
    package_root = COMMON["owned_package"](package_root, directory.with_name(directory.name + "-official") / "node_modules")
    upstream = package_root / relative_package
    plugin_path = str(Path("/official") / relative_package)
    options = spec.get("options", {})
    if set(options) - {"top_k", "timeout"}:
        raise ValueError("Mem0 deployment options are top_k and timeout")
    adapter["prepare_runtime"](Path(spec["source_config"]), directory, upstream,
        runtime_dir="/runtime", gateway_port=context["gateway_port"],
        plugin_runtime_path=plugin_path, **options)
    path = directory / "openclaw.json"
    config = json.loads(path.read_text())
    config["gateway"]["bind"] = "lan"
    adapter["save"](path, config)
    provenance_path = directory / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["base_config_sha256"] = adapter["config_digest"](config)
    adapter["save"](provenance_path, provenance)
    service = COMMON["service"](directory)
    service["volumes"].append({"type": "bind", "source": str(package_root), "target": "/official", "read_only": True})
    return {"service": service, "metadata": {"fingerprint": digest(directory)}}


def environment(binding, endpoints):
    directory = Path(binding["directory"])
    return {**COMMON["environment"](directory, endpoints),
            "MEM0_PLUGIN_STATE_FILE": str(directory / "phase.json"),
            "MEM0_GATEWAY_CONTAINER": endpoints["container"]}


def issues(binding):
    directory = Path(binding["directory"])
    result = []
    if digest(directory) != binding["metadata"]["fingerprint"]:
        result.append("Mem0 runtime configuration or official package changed")
    phase = json.loads((directory / "phase.json").read_text())
    if phase.get("phase") and (phase["phase"] != "inactive" or not phase.get("gateway_ready")):
        result.append("native Gateway has not confirmed inactive phase")
    if phase.get("active"):
        result.append("Mem0 runtime has unfinished lifecycle state")
    config = json.loads((directory / "openclaw.json").read_text())["plugins"]["entries"][PLUGIN]["config"]
    if config.get("autoCapture") or config.get("autoRecall"):
        result.append("Mem0 runtime did not restore inactive capture/recall")
    return result
