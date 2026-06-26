from __future__ import annotations

import csv
import html
import json
from pathlib import Path


def render_locomo_test_html_report(output_dir: Path) -> str:
    meta = _read_json(output_dir / "meta.json")
    diagnostics = _read_json(output_dir / "qa_diagnostics.json")
    rows = _read_rows(output_dir / "qa_results.csv")

    title = _esc(meta.get("name") or output_dir.name)
    accuracy = _pct(meta.get("overall_accuracy", 0.0))
    total_correct = _esc(meta.get("total_correct", 0))
    total_graded = _esc(meta.get("total_graded", 0))
    total_questions = _esc(meta.get("total_questions", len(rows)))
    memory_mode = _esc(meta.get("memory_mode", ""))
    closure_counts = meta.get("ov_closure_counts", {}) or {}
    closure_summary = meta.get("ov_closure_summary", {}) or {}
    issues = diagnostics.get("issues", {}) or {}
    token_totals = meta.get("memory_token_totals", {}) or {}

    focus_rows = [
        row for row in rows
        if row.get("ov_closure_state") in {"no_memory_signal", "token_emitted_only"}
        or str(row.get("result") or "").strip().upper() == "WRONG"
    ]

    def render_kv_table(data: dict) -> str:
        if not data:
            return "<p class='muted'>无</p>"
        items = "".join(
            f"<tr><td>{_esc(key)}</td><td>{_esc(value)}</td></tr>"
            for key, value in data.items()
        )
        return f"<table class='kv'><tbody>{items}</tbody></table>"

    def render_rows_table(table_rows: list[dict]) -> str:
        body = []
        for row in table_rows:
            body.append(
                "<tr>"
                f"<td>{_esc(row.get('qi'))}</td>"
                f"<td>{_esc(row.get('category'))}</td>"
                f"<td>{_esc(row.get('ov_closure_state'))}</td>"
                f"<td>{_esc(row.get('ov_recall_total'))}</td>"
                f"<td>{_esc(row.get('result'))}</td>"
                f"<td>{_esc(row.get('question'))}</td>"
                f"<td>{_esc(row.get('response'))}</td>"
                f"<td>{_esc(row.get('reasoning'))}</td>"
                "</tr>"
            )
        if not body:
            body.append("<tr><td colspan='8' class='muted'>无</td></tr>")
        return (
            "<table class='rows'><thead><tr>"
            "<th>Q</th><th>分类</th><th>Closure</th><th>Recall</th><th>Judge</th>"
            "<th>Question</th><th>Response</th><th>Reasoning</th>"
            "</tr></thead><tbody>"
            + "".join(body)
            + "</tbody></table>"
        )

    category_rows = "".join(
        f"<tr><td>{_esc(cat)}</td><td>{_esc(data.get('correct'))}</td><td>{_esc(data.get('total'))}</td><td>{_pct(data.get('accuracy', 0))}</td></tr>"
        for cat, data in sorted((meta.get("accuracy_by_category") or {}).items())
    ) or "<tr><td colspan='4' class='muted'>无</td></tr>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} - Test Report</title>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #111827;
      --panel-2: #1f2937;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --ok: #34d399;
      --warn: #f59e0b;
      --bad: #f87171;
      --line: #334155;
      --accent: #38bdf8;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; background: linear-gradient(180deg, #020617, var(--bg)); color: var(--text); }}
    .wrap {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .muted {{ color: var(--muted); }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin: 20px 0; }}
    .card {{ background: rgba(17,24,39,.92); border: 1px solid var(--line); border-radius: 14px; padding: 16px; }}
    .metric {{ font-size: 28px; font-weight: 700; margin-top: 8px; }}
    .section {{ margin-top: 24px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; vertical-align: top; padding: 10px 12px; border-bottom: 1px solid var(--line); }}
    th {{ color: var(--muted); font-weight: 600; background: rgba(148,163,184,.06); position: sticky; top: 0; }}
    .rows td:nth-child(6), .rows td:nth-child(7), .rows td:nth-child(8) {{ max-width: 0; word-break: break-word; }}
    .pill {{ display: inline-block; padding: 4px 8px; border-radius: 999px; background: rgba(56,189,248,.12); color: var(--accent); }}
    .warn {{ color: var(--warn); }}
    .ok {{ color: var(--ok); }}
    .bad {{ color: var(--bad); }}
    @media (max-width: 960px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
    @media (max-width: 640px) {{ .grid {{ grid-template-columns: 1fr; }} .wrap {{ padding: 16px; }} }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{title}</h1>
    <div class="muted">LoCoMo / OpenClaw / OpenViking 测试报告</div>

    <div class="grid">
      <div class="card"><div class="muted">Overall Accuracy</div><div class="metric ok">{accuracy}</div></div>
      <div class="card"><div class="muted">Correct / Graded</div><div class="metric">{total_correct} / {total_graded}</div></div>
      <div class="card"><div class="muted">Total Questions</div><div class="metric">{total_questions}</div></div>
      <div class="card"><div class="muted">Memory Mode</div><div class="metric">{memory_mode}</div></div>
    </div>

    <div class="section grid">
      <div class="card">
        <h2>Closure Summary</h2>
        {render_kv_table(closure_summary)}
      </div>
      <div class="card">
        <h2>Closure Counts</h2>
        {render_kv_table(closure_counts)}
      </div>
      <div class="card">
        <h2>Diagnostics</h2>
        {render_kv_table(issues)}
      </div>
        <div class="card">
          <h2>OV Tokens</h2>
          {render_kv_table({
          'provider': token_totals.get('provider', ''),
          'llm_total': token_totals.get('llm_total', 0),
          'embedding': token_totals.get('embedding', 0),
          'memories': token_totals.get('memories', 0),
        })}
        </div>
    </div>

    <div class="section card">
      <h2>Accuracy By Category</h2>
      <table>
        <thead><tr><th>Category</th><th>Correct</th><th>Total</th><th>Accuracy</th></tr></thead>
        <tbody>{category_rows}</tbody>
      </table>
    </div>

    <div class="section card">
      <h2>Focus Rows</h2>
      <div class="muted">聚焦 `no_memory_signal`、`token_emitted_only` 以及 judge=WRONG 的问题。</div>
      {render_rows_table(focus_rows)}
    </div>

    <div class="section card">
      <h2>All Rows</h2>
      {render_rows_table(rows)}
    </div>
  </div>
</body>
</html>"""


def write_locomo_test_html_report(output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    report_path = output_dir / "report.html"
    report_path.write_text(render_locomo_test_html_report(output_dir), encoding="utf-8")
    return report_path


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _pct(value: float | int | None) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return "0.00%"


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))
