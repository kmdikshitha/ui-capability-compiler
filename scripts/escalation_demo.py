"""Drive the full escalation cycle end to end, with a stand-in operator.

What is being demonstrated: the automation pauses, a *separate process* takes
the same live browser session over CDP, does the manual work, and signals resume
by appending to control.jsonl -- the same file the paused run is polling.

The operator here is a thread so the whole cycle runs unattended. In real use it
is a person at the console (`uv run python -m src.cli console`), and nothing on
the automation side of the seam knows the difference.

    uv run python scripts/escalation_demo.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright                       # noqa: E402

from src.artifact import store                                        # noqa: E402
from src.escalation.requests import (                                 # noqa: E402
    InterventionRequest, read as read_request, write as write_request)
from src.escalation.state import ControlLog                           # noqa: E402
from src.evidence.recorder import Recorder                            # noqa: E402
from src.guardrails.allowlist import Policy                           # noqa: E402
from src.replay.executor import Executor                              # noqa: E402
from src.surface.playwright_driver import PlaywrightSurface           # noqa: E402
from src.types import Action                                          # noqa: E402

CDP_PORT = 9223
RUN_ID = "escalation"


def inject(mode: str) -> None:
    request = urllib.request.Request(
        "http://localhost:8000/admin/inject", data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10).read()


def serve() -> subprocess.Popen:
    subprocess.run(["pkill", "-f", "uvicorn mock_app.main"], capture_output=True)
    time.sleep(1)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_app.main:app", "--port", "8000",
         "--log-level", "warning"],
        cwd=str(ROOT), env={**os.environ, "MOCK_TENANT": "heritage"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://localhost:8000/login", timeout=1)
            return process
        except Exception:
            time.sleep(0.5)
    raise SystemExit("mock app did not start")


def operator(run_dir: Path) -> None:
    """Stands in for a human at the operator console."""
    for _ in range(180):
        request = read_request(run_dir)
        if request and request.status == "open":
            break
        time.sleep(0.5)
    else:
        print("   [operator] no request appeared")
        return
    print(f"   [operator] picked up request: {request.reason}")

    # Attach to the SAME live session over CDP -- not a fresh browser.
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://localhost:{CDP_PORT}")
        page = browser.contexts[0].pages[0]
        print(f"   [operator] attached over CDP, sees: {page.url}")
        work = page.frames[-1]
        work.get_by_role("textbox").first.fill("SUP-9931")
        print("   [operator] entered the override code the agent could not")
        work.get_by_role("button", name="Approve").click()
        page.wait_for_load_state("domcontentloaded")
        print(f"   [operator] dialog resolved, now on: {page.url}")
        browser.close()

    # The resume signal IS the audit record.
    ControlLog(run_dir / "control.jsonl").transfer(
        "agent", reason="operator resolved supervisor override",
        step=request.step_id, by="operator")
    request.status = "resumed"
    write_request(request, run_dir)
    print("   [operator] handed control back")


def main() -> int:
    server = serve()
    try:
        inject("clear")
        artifact = store.load(ROOT / "artifacts" / "member.open_subaccount.v1.json")
        artifact.status = "approved"

        recorder = Recorder(RUN_ID, base=ROOT / "runs")
        for name in ("control.jsonl", "steps.jsonl", "intervention.json"):
            (recorder.dir / name).unlink(missing_ok=True)
        control = ControlLog(recorder.path("control.jsonl"))

        # A persistent, remote-debuggable browser: a separate OS process that
        # outlives the decision loop.
        surface = PlaywrightSurface(
            headless=True, user_data_dir=str(ROOT / "runs" / "browser-profile-escalation"),
            remote_debugging_port=CDP_PORT)

        def on_escalate(reason: str, step_id: str, risk: str, wait: bool) -> bool:
            shot = surface.screenshot(recorder.screenshot_path(f"intervention-{step_id}"))
            write_request(InterventionRequest.open(
                id=RUN_ID, run_id=RUN_ID, capability_id=artifact.capability_id,
                version=artifact.version, goal=artifact.description, step_id=step_id,
                risk=risk, reason=reason, url=surface.page.url, screenshot_path=shot,
                cdp_url=f"http://localhost:{CDP_PORT}"), recorder.dir)
            if not wait:
                return False
            control.transfer("human", reason=reason, step=step_id)
            print(f"   [agent]    paused at {step_id}: {reason}")
            print("   [agent]    browser stays open; waiting for control back...")
            resumed = control.wait_for_agent(timeout_s=120)
            print(f"   [agent]    control returned: {resumed}")
            return resumed

        threading.Thread(target=operator, args=(recorder.dir,), daemon=True).start()

        try:
            surface.act(Action(type="navigate", url="http://localhost:8000/login"))
            obs = surface.observe()
            surface.act(Action(type="click",
                               ref=next(e.ref for e in obs.elements if e.role == "button")))
            inject("unknown_dialog")      # armed so it lands mid-flow
            executor = Executor(
                surface, artifact, policy=Policy.load(), attended=True,
                record_step=recorder.step, screenshot_path=recorder.screenshot_path,
                on_escalate=on_escalate, run_id=RUN_ID, control=control)
            result = executor.run({"member_id": "54321", "account_type": "Savings",
                                   "initial_deposit": "99.50", "nickname": "Holiday"})
        finally:
            surface.close()

        recorder.result(result.model_dump())
        print(f"\n   final status: {result.status}  exit {result.exit_code}")
        print(f"   outputs: {result.outputs}")
        print("\n   control.jsonl (the transport, not just a log):")
        for record in control.records():
            print(f"     {record['from']:6} -> {record['to']:6}  by {record['by']:8} "
                  f"at {record['step']}  ({record['reason']})")
        return 0 if result.status == "success" else 1
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
