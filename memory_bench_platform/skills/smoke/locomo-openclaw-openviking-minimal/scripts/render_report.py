from __future__ import annotations

import html
import json
import sys


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    manifest = payload.get("manifest", {}) or {}
    validation = payload.get("validation", {}) or {}
    stage_results = validation.get("stage_results", []) or []
    issues = validation.get("issues", []) or []
    rows = "".join(
        "<tr>"
        f"<td>{_esc(item.get('question'))}</td>"
        f"<td>{_esc(item.get('label'))}</td>"
        f"<td>{_esc(item.get('response'))}</td>"
        "</tr>"
        for item in stage_results
    ) or "<tr><td colspan='3'>无</td></tr>"
    issue_list = "".join(f"<li>{_esc(issue)}</li>" for issue in issues) or "<li>无</li>"
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>{_esc(manifest.get('id'))} smoke report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #1f2937; }}
    .ok {{ color: #0f766e; }}
    .bad {{ color: #b91c1c; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ border-bottom: 1px solid #d1d5db; padding: 8px 10px; text-align: left; vertical-align: top; }}
  </style>
</head>
<body>
  <h1>{_esc(manifest.get('id'))}</h1>
  <p>status:
    <strong class="{'ok' if validation.get('status') == 'passed' else 'bad'}">{_esc(validation.get('status'))}</strong>
  </p>
  <h2>Stages</h2>
  <table>
    <thead><tr><th>Stage</th><th>Status</th><th>Detail</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <h2>Issues</h2>
  <ul>{issue_list}</ul>
</body>
</html>"""
    print(json.dumps({"html": html_text}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
