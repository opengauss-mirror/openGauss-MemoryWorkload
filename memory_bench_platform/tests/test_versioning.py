from memory_bench_platform.versioning import _tag_sort_key, build_version_selection, resolve_latest_release_tag


class _Target:
    def __init__(self, payload):
        self.version_source = payload["version_source"]
        self.upstream = payload.get("upstream")
        self._payload = payload

    def model_dump(self, mode="json"):
        return dict(self._payload)


class _Policy:
    def __init__(self, targets):
        self.default_selection = "latest_official_release_tag"
        self.targets = [_Target(item) for item in targets]


class _Manifest:
    def __init__(self, targets):
        self.version_policy = _Policy(targets)


def test_tag_sort_key_ignores_non_release_tags():
    assert _tag_sort_key("v0.3.24") == (0, 3, 24)
    assert _tag_sort_key("1.2.3") == (1, 2, 3)
    assert _tag_sort_key("v0.3.24-rc1") is None
    assert _tag_sort_key("main") is None


def test_resolve_latest_release_tag_picks_highest_semver(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "\n".join(
            [
                "sha1\trefs/tags/v0.3.9",
                "sha2\trefs/tags/v0.3.24",
                "sha3\trefs/tags/v0.3.24-rc1",
                "sha4\trefs/tags/v0.3.18",
            ]
        )
        stderr = ""

    monkeypatch.setattr("memory_bench_platform.versioning.subprocess.run", lambda *args, **kwargs: _Proc())
    resolved = resolve_latest_release_tag("https://github.com/volcengine/OpenViking")
    assert resolved["status"] == "resolved"
    assert resolved["resolved_version"] == "v0.3.24"


def test_build_version_selection_records_resolved_default(monkeypatch):
    monkeypatch.setattr(
        "memory_bench_platform.versioning.resolve_latest_release_tag",
        lambda upstream, **kwargs: {
            "status": "resolved",
            "upstream": upstream,
            "resolved_version": "v9.9.9",
            "source": "test",
        },
    )
    manifest = _Manifest(
        [
            {
                "name": "openclaw",
                "scope": "system_under_test",
                "version_source": "upstream_release_tag",
                "upstream": "https://github.com/openclaw/openclaw",
            },
            {
                "name": "generic-cli",
                "scope": "runtime_dependency",
                "version_source": "runtime_observed_only",
                "upstream": None,
            },
        ]
    )
    payload = build_version_selection(manifest)
    assert payload["selection_mode"] == "latest_official_release_tag"
    assert payload["targets"][0]["resolved_default"]["resolved_version"] == "v9.9.9"
    assert payload["targets"][1]["resolved_default"]["status"] == "runtime_observed_only"
