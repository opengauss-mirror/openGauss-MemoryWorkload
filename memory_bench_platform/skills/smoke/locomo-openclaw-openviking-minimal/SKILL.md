# LoCoMo OpenClaw OpenViking Minimal Smoke

## Purpose

This smoke skill validates the minimum runnable chain before a full benchmark run:

1. session bootstrap
2. ingest one minimal conversation
3. commit and wait for extraction
4. verify memory diff exists
5. verify recall hits
6. ask one answer probe
7. verify usage and result parsing

## Expected Outcome

- The platform can distinguish platform integration failures from model-quality failures.
- A failing smoke run should block or downgrade the corresponding full benchmark run.

## Output

Expected platform artifacts:

- `smoke_summary.json`
- `smoke_trace.json`
- `smoke_report.html`

## Notes

This directory is a manifest skeleton for the smoke skill layer. The platform loader and runner still need explicit smoke-skill support before this skill becomes executable.
