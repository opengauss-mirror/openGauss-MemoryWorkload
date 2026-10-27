from memory_bench_platform.deployment_golden import MARKER


def test_digest_tracks_nested_content_and_missing_files(tmp_path):
    from memory_bench_platform.deployment_volume_digest import digest_tree

    nested = tmp_path / "etcd" / "member"
    nested.mkdir(parents=True)
    data = nested / "data"
    data.write_bytes(b"baseline")
    original = digest_tree(tmp_path, MARKER)

    (tmp_path / MARKER).write_text('{"version":1}')
    assert digest_tree(tmp_path, MARKER) == original
    data.write_bytes(b"modified")
    assert digest_tree(tmp_path, MARKER) != original
    data.unlink()
    assert digest_tree(tmp_path, MARKER) != original
