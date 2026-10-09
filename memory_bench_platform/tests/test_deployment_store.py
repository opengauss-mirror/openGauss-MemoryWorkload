import pytest

from memory_bench_platform.deployment_store import DeploymentLocked, DeploymentState, DeploymentStore


def test_atomic_state_and_lock(tmp_path):
    store = DeploymentStore(tmp_path)
    store.create("suite")
    state = DeploymentState(name="suite", config_path="/tmp/deploy.yaml", config_sha256="abc")
    store.write_atomic(state)
    assert store.read("suite").config_sha256 == "abc"
    assert "PRIVATE_TOKEN" not in store.read("suite").model_dump_json()
    store.acquire_lock("suite")
    with pytest.raises(DeploymentLocked):
        DeploymentStore(tmp_path).acquire_lock("suite")
    store.release_lock("suite")

