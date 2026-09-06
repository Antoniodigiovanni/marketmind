"""Server-rendered HTML for the operations dashboard."""

from __future__ import annotations

import datetime as dt
import html
import json
from typing import Any

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1c1b19; --muted: #6b6862;
  --line: #e3e0da; --card: #ffffff;
  --ok: #2f7d51; --bad: #b3352b; --warn: #a4681c; --run: #2a5d9e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #171714; --fg: #eceae5; --muted: #9a958c;
    --line: #302e29; --card: #1f1e1a;
    --ok: #63b184; --bad: #e0776c; --warn: #d09a4e; --run: #7aa5d8;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 32px 24px 64px; }
h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 40px 0 12px; font-weight: 600; }
.sub { color: var(--muted); margin: 0 0 8px; }
a { color: inherit; }
.cards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.card .job { font-weight: 600; margin-bottom: 6px; }
.card .when { color: var(--muted); font-size: 12px; }
.status { font-weight: 600; }
.status-succeeded { color: var(--ok); }
.status-failed, .status-aborted { color: var(--bad); }
.status-started { color: var(--run); }
.status-missing { color: var(--warn); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top; font-variant-numeric: tabular-nums; }
th { color: var(--muted); font-weight: 600; font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.06em; }
.scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--card); }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
code { font-size: 12px; color: var(--muted); }
pre.log { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px; overflow-x: auto; font-size: 12px; line-height: 1.55; margin: 0; }
.err { color: var(--bad); }
.note { color: var(--muted); font-size: 12px; margin-top: 8px; }
.back { display: inline-block; margin-bottom: 16px; color: var(--muted); font-size: 13px; }
"""


def _page(title: str, body: str) -> str:
    """Wrap rendered body markup in the shared document shell."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _ago(value: dt.datetime | None) -> str:
    """Render a timestamp as a compact relative age."""
    if value is None:
        return "—"
    delta = dt.datetime.now(dt.UTC) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


def _duration(row: dict[str, Any]) -> str:
    """Render a run's elapsed time, or its age while still running."""
    started, finished = row.get("started_at"), row.get("finished_at")
    if started is None:
        return "—"
    end = finished or dt.datetime.now(dt.UTC)
    seconds = int((end - started).total_seconds())
    rendered = f"{seconds}s" if seconds < 120 else f"{seconds // 60}m {seconds % 60}s"
    return rendered if finished else f"{rendered}…"


def _counters(value: Any) -> str:
    """Render counters JSON compactly, escaped."""
    if not value:
        return "<code>—</code>"
    return f"<code>{html.escape(json.dumps(value, sort_keys=True, default=str))}</code>"


def render_overview(
    *,
    runs: list[dict[str, Any]],
    expected_jobs: list[str],
    logs: list[dict[str, Any]],
    problems: list[str],
    db_error: str | None = None,
) -> str:
    """Render the run overview page.

    Args:
        runs: Recent agent_runs rows, newest first.
        expected_jobs: Job types that should appear; missing ones are called
            out rather than silently absent.
        logs: Log file descriptors from dashboard.logs.list_logs.
        problems: Human-readable log directory problems.
        db_error: Message shown instead of run data when the query failed.

    Returns:
        A complete HTML document.
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in runs:
        latest.setdefault(str(row["job_type"]), row)

    cards = []
    for job in expected_jobs:
        row = latest.get(job)
        if row is None:
            cards.append(
                f"<div class='card'><div class='job'>{html.escape(job)}</div>"
                "<div class='status status-missing'>never run</div>"
                "<div class='when'>no audit row</div></div>"
            )
            continue
        status = str(row["status"])
        cards.append(
            f"<div class='card'><div class='job'>{html.escape(job)}</div>"
            f"<div class='status status-{html.escape(status)}'>{html.escape(status)}</div>"
            f"<div class='when'>{html.escape(_ago(row.get('started_at')))}"
            f" · {html.escape(_duration(row))}</div></div>"
        )

    if db_error is not None:
        body_rows = f"<tr><td colspan='7' class='err'>{html.escape(db_error)}</td></tr>"
    elif not runs:
        body_rows = "<tr><td colspan='7'>No runs recorded yet.</td></tr>"
    else:
        body_rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(row['job_type']))}</td>"
            f"<td class='status status-{html.escape(str(row['status']))}'>"
            f"{html.escape(str(row['status']))}</td>"
            f"<td>{html.escape(str(row['run_date']))}</td>"
            f"<td>{html.escape(_ago(row.get('started_at')))}</td>"
            f"<td>{html.escape(_duration(row))}</td>"
            f"<td>{_counters(row.get('counters'))}</td>"
            f"<td class='err'>{html.escape(str(row.get('error') or ''))}</td>"
            "</tr>"
            for row in runs
        )

    if logs:
        log_rows = "".join(
            "<tr>"
            f"<td><a href='/logs/{html.escape(str(entry['name']))}'>"
            f"{html.escape(str(entry['name']))}</a></td>"
            f"<td>{html.escape(str(entry['directory']))}</td>"
            f"<td>{int(entry['size_bytes']):,} B</td>"
            f"<td>{html.escape(_ago(entry['modified_at']))}</td>"
            "</tr>"
            for entry in logs
        )
    else:
        log_rows = "<tr><td colspan='4'>No log files visible.</td></tr>"

    notes = "".join(f"<div class='note'>{html.escape(problem)}</div>" for problem in problems)

    body = (
        "<h1>marketmind — scheduled jobs</h1>"
        "<p class='sub'>Read-only view of run history and job logs.</p>"
        "<h2>Last run per job</h2>"
        f"<div class='cards'>{''.join(cards)}</div>"
        "<h2>Recent runs</h2>"
        "<div class='scroll'><table><thead><tr><th>Job</th><th>Status</th><th>Date</th>"
        "<th>Started</th><th>Duration</th><th>Counters</th><th>Error</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div>"
        "<h2>Logs</h2>"
        "<div class='scroll'><table><thead><tr><th>File</th><th>Directory</th>"
        f"<th>Size</th><th>Modified</th></tr></thead><tbody>{log_rows}</tbody></table></div>"
        f"{notes}"
    )
    return _page("marketmind — scheduled jobs", body)


def render_log(*, name: str, text: str, truncated: bool) -> str:
    """Render one log file's tail.

    Args:
        name: Log file name.
        text: Tail contents.
        truncated: Whether earlier content was omitted.

    Returns:
        A complete HTML document.
    """
    note = (
        "<div class='note'>Showing the end of the file; earlier lines omitted.</div>"
        if truncated
        else ""
    )
    body = (
        "<a class='back' href='/'>← all jobs</a>"
        f"<h1>{html.escape(name)}</h1>"
        f"{note}"
        f"<pre class='log'>{html.escape(text)}</pre>"
    )
    return _page(f"{name} — marketmind", body)
