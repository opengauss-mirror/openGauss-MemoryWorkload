from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from memory_bench_platform.trace_runtime.bundle import BundleWriter, load_bundle
from memory_bench_platform.trace_runtime.protocol import (
    ProviderRequest,
    ProviderResponse,
    TraceBundleChannel,
    TraceBundleManifest,
    TraceRecord,
    TraceScope,
    TraceTiming,
)
from memory_bench_platform.trace_runtime.redaction import redact_headers, redact_json


def _record(*, request_id: str = "request-1") -> TraceRecord:
    return TraceRecord(
        trace_id="trace-1",
        ordinal=1,
        channel="memory_extract",
        protocol="openai-chat-completions",
        scope=TraceScope(request_id=request_id, session_id="session-1"),
        request=ProviderRequest(
            method="POST",
            path="/v1/chat/completions",
            headers={"content-type": "application/json"},
            body={"model": "test", "messages": [{"role": "user", "content": "hello"}]},
        ),
        response=ProviderResponse(
            status=200,
            headers={"content-type": "application/json"},
            body={"choices": [{"message": {"content": "world"}}]},
        ),
        timing=TraceTiming(
            started_at="2026-09-14T00:00:00+00:00",
            upstream_duration_ms=12.5,
        ),
    )


def test_bundle_writer_finalizes_atomically_and_loads(tmp_path: Path):
    output = tmp_path / "trace-bundle"
    writer = BundleWriter(
        output,
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        channels={
            "memory_extract": {
                "protocol": "openai-chat-completions",
                "match_mode": "fingerprint",
                "order_scope": "fingerprint",
            }
        },
    )
    writer.append(_record())
    manifest = writer.finalize()

    loaded = load_bundle(output)
    assert manifest.bundle_id == "bundle-1"
    assert loaded.manifest.channels["memory_extract"].record_count == 1
    assert loaded.records["memory_extract"][0].scope.request_id == "request-1"
    assert not list(tmp_path.glob(".trace-bundle.tmp-*"))


def test_bundle_rejects_modified_record_file(tmp_path: Path):
    output = tmp_path / "trace-bundle"
    writer = BundleWriter(
        output,
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        channels={
            "memory_extract": {
                "protocol": "openai-chat-completions",
                "match_mode": "fingerprint",
                "order_scope": "fingerprint",
            }
        },
    )
    writer.append(_record())
    writer.finalize()
    records_path = output / "memory_extract" / "records.jsonl"
    records_path.write_text(records_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sha256"):
        load_bundle(output)


def test_redaction_removes_credentials_without_changing_other_fields():
    headers = redact_headers(
        {
            "Authorization": "Bearer secret",
            "X-API-Key": "secret",
            "Content-Type": "application/json",
        }
    )
    body = redact_json(
        {
            "access_token": "secret",
            "nested": {"refresh_token": "secret", "model": "test"},
        }
    )

    assert headers == {"content-type": "application/json"}
    assert body == {
        "access_token": "[REDACTED]",
        "nested": {"refresh_token": "[REDACTED]", "model": "test"},
    }


def test_redaction_removes_credentials_embedded_in_strings():
    body = redact_json(
        {
            "error": "request failed for Bearer abc123 at https://x.test?a=1&api_key=secret",
            "clientSecret": "secret",
        }
    )

    assert body == {
        "error": "request failed for Bearer [REDACTED] at https://x.test?a=1&api_key=[REDACTED]",
        "clientSecret": "[REDACTED]",
    }


def test_redaction_applies_json_pointer_rules():
    body = redact_json(
        {"metadata": {"tenant_credential": "secret", "keep": "value"}},
        ["/metadata/tenant_credential"],
    )

    assert body == {
        "metadata": {"tenant_credential": "[REDACTED]", "keep": "value"}
    }


def test_bundle_rejects_duplicate_request_ids(tmp_path: Path):
    output = tmp_path / "trace-bundle"
    writer = BundleWriter(
        output,
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        channels={
            "memory_extract": {
                "protocol": "openai-chat-completions",
                "match_mode": "fingerprint",
                "order_scope": "fingerprint",
            }
        },
    )
    writer.append(_record())

    with pytest.raises(ValueError, match="duplicate request_id"):
        writer.append(_record())


def test_unknown_record_schema_version_is_rejected(tmp_path: Path):
    output = tmp_path / "trace-bundle"
    writer = BundleWriter(
        output,
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        channels={
            "memory_extract": {
                "protocol": "openai-chat-completions",
                "match_mode": "fingerprint",
                "order_scope": "fingerprint",
            }
        },
    )
    writer.append(_record())
    writer.finalize()
    records_path = output / "memory_extract" / "records.jsonl"
    payload = json.loads(records_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "trace-record/999"
    records_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    import hashlib

    manifest["channels"]["memory_extract"]["sha256"] = hashlib.sha256(
        records_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_bundle(output)


def test_trace_json_schemas_are_valid_and_accept_models():
    schemas_root = Path(__file__).resolve().parents[1] / "schemas"
    record_schema = json.loads(
        (schemas_root / "trace-record.schema.json").read_text(encoding="utf-8")
    )
    manifest_schema = json.loads(
        (schemas_root / "trace-manifest.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(record_schema)
    Draft202012Validator.check_schema(manifest_schema)
    Draft202012Validator(record_schema).validate(_record().model_dump(mode="json"))
    manifest = TraceBundleManifest(
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        created_at="2026-09-14T00:00:00+00:00",
        channels={
            "memory_extract": TraceBundleChannel(
                protocol="openai-chat-completions",
                records="memory_extract/records.jsonl",
                record_count=1,
                sha256="0" * 64,
                match_mode="fingerprint",
                order_scope="fingerprint",
            )
        },
    )
    Draft202012Validator(manifest_schema).validate(manifest.model_dump(mode="json"))


def test_bundle_rejects_records_path_outside_bundle(tmp_path: Path):
    output = tmp_path / "trace-bundle"
    writer = BundleWriter(
        output,
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        channels={
            "memory_extract": {
                "protocol": "openai-chat-completions",
                "match_mode": "fingerprint",
                "order_scope": "fingerprint",
            }
        },
    )
    writer.append(_record())
    writer.finalize()
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["channels"]["memory_extract"]["records"] = "../outside.jsonl"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="escapes bundle root"):
        load_bundle(output)


def test_bundle_writer_rejects_unredacted_secret(tmp_path: Path):
    writer = BundleWriter(
        tmp_path / "trace-bundle",
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        channels={
            "memory_extract": {
                "protocol": "openai-chat-completions",
                "match_mode": "fingerprint",
                "order_scope": "fingerprint",
            }
        },
    )
    record = _record()
    record.response.body["access_token"] = "unredacted"

    with pytest.raises(ValueError, match="trace_secret_detected"):
        writer.append(record)
    writer.discard()


def test_bundle_writer_rejects_secret_hidden_in_raw_sse_frame(tmp_path: Path):
    import base64

    writer = BundleWriter(
        tmp_path / "trace-bundle",
        bundle_id="bundle-1",
        profile_id="openai-compatible@1",
        channels={
            "memory_extract": {
                "protocol": "openai-chat-completions",
                "match_mode": "fingerprint",
                "order_scope": "fingerprint",
            }
        },
    )
    record = _record()
    record.response.raw_body_base64 = base64.b64encode(
        b'data: {"access_token":"unredacted"}\n\n'
    ).decode()

    with pytest.raises(ValueError, match="trace_secret_detected"):
        writer.append(record)
    writer.discard()
