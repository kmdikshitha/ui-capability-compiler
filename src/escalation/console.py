"""The operator console.

Deliberately plain. The styling is mocked; the control transfer is real. An
operator opens a request, attaches to the live browser session the automation
is sitting in, does what needs doing, and hands control back -- and the handing
back is an append to control.jsonl, which is the same file the waiting CLI is
polling.

Scope note: full real-time co-browsing is out of scope per the brief. The
operator is local and attaches over CDP to a browser that is already on screen.
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from src.escalation.requests import InterventionRequest, open_requests, read, set_status
from src.escalation.state import ControlLog

RUNS_DIR = Path("runs")

app = FastAPI(title="Operator console")


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>{html.escape(title)}</title>
<style>
 body {{ font: 14px -apple-system, Segoe UI, sans-serif; margin: 2rem; max-width: 860px;
         color: #1a1a1a; }}
 h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
 table {{ border-collapse: collapse; width: 100%; margin: .6rem 0 1.2rem; }}
 td, th {{ border: 1px solid #ddd; padding: .45rem .6rem; text-align: left;
           vertical-align: top; }}
 th {{ background: #f6f6f6; width: 11rem; font-weight: 600; }}
 code {{ background: #f2f2f2; padding: .12rem .3rem; border-radius: 3px; }}
 pre {{ background: #f6f6f6; padding: .7rem; border-radius: 4px; overflow-x: auto; }}
 .risk-irreversible {{ color: #a3000b; font-weight: 600; }}
 button {{ font-size: .95rem; padding: .45rem .9rem; margin-right: .5rem; cursor: pointer; }}
 img {{ max-width: 100%; border: 1px solid #ccc; }}
 .muted {{ color: #666; font-size: .9em; }}
 .do {{ background: #eef6ff; border-left: 3px solid #2b6cb0; padding: .6rem .8rem; }}
</style></head><body>{body}</body></html>""")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    requests = [r for r in open_requests(RUNS_DIR) if r.status == "open"]
    if not requests:
        rows = '<p class="muted">No open intervention requests.</p>'
    else:
        rows = "<table><tr><th>Run</th><th>Capability</th><th>Step</th>" \
               "<th>Why it stopped</th></tr>"
        for item in requests:
            rows += (
                f'<tr><td><a href="/request/{html.escape(item.id)}">'
                f'{html.escape(item.run_id)}</a></td>'
                f"<td>{html.escape(item.capability_id)} v{item.version}</td>"
                f"<td>{html.escape(item.step_id)} "
                f'<span class="risk-{item.risk}">{html.escape(item.risk)}</span></td>'
                f"<td>{html.escape(item.reason)}</td></tr>"
            )
        rows += "</table>"
    return _page("Operator console", f"<h1>Open intervention requests</h1>{rows}")


def _find(request_id: str) -> tuple[InterventionRequest, Path] | tuple[None, None]:
    for run_dir in RUNS_DIR.iterdir() if RUNS_DIR.exists() else []:
        item = read(run_dir)
        if item and item.id == request_id:
            return item, run_dir
    return None, None


@app.get("/request/{request_id}", response_class=HTMLResponse)
def detail(request_id: str):
    item, run_dir = _find(request_id)
    if item is None:
        return _page("Not found", "<h1>No such request</h1><p><a href='/'>Back</a></p>")

    controller = ControlLog(run_dir / "control.jsonl").controller()
    shot = ""
    if item.screenshot_path and Path(item.screenshot_path).exists():
        shot = (f"<h2>What the automation was looking at</h2>"
                f'<img src="/request/{html.escape(request_id)}/screenshot" alt="last screen">')

    return _page(f"Intervention {item.run_id}", f"""
<p><a href="/">&larr; All requests</a></p>
<h1>Intervention required</h1>
<table>
  <tr><th>Capability</th><td>{html.escape(item.capability_id)} v{item.version}</td></tr>
  <tr><th>Goal</th><td>{html.escape(item.goal)}</td></tr>
  <tr><th>Stopped at</th><td>{html.escape(item.step_id)}
      (<span class="risk-{item.risk}">{html.escape(item.risk)}</span>)</td></tr>
  <tr><th>Why</th><td>{html.escape(item.reason)}</td></tr>
  <tr><th>Page</th><td><code>{html.escape(item.url)}</code></td></tr>
  <tr><th>Control</th><td><b>{html.escape(controller)}</b></td></tr>
  <tr><th>Parameters</th>
      <td class="muted">Withheld. Input values are regulated data and are not
      copied into intervention requests; read what you need off the session.</td></tr>
</table>

<h2>Take control of the live session</h2>
<p>The automation is paused and is <b>not</b> sending commands. The browser it
was driving is still open on this machine with the same tab, cookies and scroll
position.</p>
<p class="do"><b>Switch to that Chromium window on your desktop</b> and work in
it directly. Do what the run cannot do, leave the browser on the screen the run
should continue from, then press the button below.</p>
<p class="muted">Automating the handover instead? The session exposes a Chrome
DevTools Protocol endpoint at <code>{html.escape(item.cdp_url)}</code> for
<code>connect_over_cdp()</code>. That is a machine-to-machine API, not a page
&mdash; opening it in a browser will not show you the session, and nothing may
listen on that port until a <code>--headed</code> run is paused.</p>

<form method="post" action="/request/{html.escape(request_id)}/resume"
      style="display:inline">
  <button type="submit">Hand control back to the agent</button>
</form>
<form method="post" action="/request/{html.escape(request_id)}/abort"
      style="display:inline">
  <button type="submit">Abort this run</button>
</form>
{shot}
""")


@app.get("/request/{request_id}/screenshot")
def screenshot(request_id: str):
    item, _ = _find(request_id)
    if item is None or not item.screenshot_path or not Path(item.screenshot_path).exists():
        return HTMLResponse("no screenshot", status_code=404)
    return FileResponse(item.screenshot_path, media_type="image/png")


@app.post("/request/{request_id}/resume")
def resume(request_id: str):
    item, run_dir = _find(request_id)
    if item is None:
        return RedirectResponse("/", status_code=303)
    # This append IS the resume signal. The waiting CLI polls this same file,
    # so the audit trail and the control channel cannot disagree.
    ControlLog(run_dir / "control.jsonl").transfer(
        "agent", reason="operator resumed", step=item.step_id, by="operator")
    set_status(run_dir, "resumed")
    return RedirectResponse("/", status_code=303)


@app.post("/request/{request_id}/abort")
def abort(request_id: str):
    item, run_dir = _find(request_id)
    if item is None:
        return RedirectResponse("/", status_code=303)
    ControlLog(run_dir / "control.jsonl").transfer(
        "none", reason="operator aborted", step=item.step_id, by="operator")
    set_status(run_dir, "aborted")
    return RedirectResponse("/", status_code=303)
