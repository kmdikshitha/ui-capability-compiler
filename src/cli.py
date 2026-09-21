"""Command line entry points.

    discover   run the model against a live surface and compile what it found
    replay     execute a compiled capability with no model in the loop
    approve    promote a reviewed draft so it may run unattended
    console    the operator console for intervention requests
    show       print an artifact for a human
    drift      compare the last run's locator rungs against the recording
    validate   check an artifact loads and its contract hash still matches

replay never reads ANTHROPIC_API_KEY. That is the thesis of the project and it
doubles as the answer to "how do I run this without live services".
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from pathlib import Path
from typing import Any

import typer
import yaml

from src.artifact import store
from src.artifact.compiler import compile_artifact, load_spec
from src.artifact.models import CapabilityArtifact, ContractViolation, LADDER
from src.artifact.overrides import apply_override, load_override
from src.evidence.recorder import Recorder, new_run_id, read_jsonl
from src.guardrails.allowlist import Policy
from src.guardrails.redaction import RedactionFilter, register_sensitive
from src.replay.executor import Executor
from src.replay.checkpoints import describe as describe_checkpoint
from src.replay.outcomes import EXIT_CODES
from src.surface.playwright_driver import PlaywrightSurface

app = typer.Typer(add_completion=False, help=__doc__)

CDP_PORT = 9222
CONSOLE_PORT = 5056
BROWSER_PROFILE = "runs/browser-profile"


def _target_reachable(url: str = "http://localhost:8000/login") -> bool:
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except Exception:
        return False


def _console_running(port: int = CONSOLE_PORT) -> bool:
    """Is an operator console actually listening?

    Printing a console URL that nothing is serving sends an operator to a dead
    link at the exact moment a run is blocked waiting for them.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _load_env(path: str | Path = ".env") -> None:
    """Load .env into the environment, without adding a dependency.

    Values already set in the real environment win, so an exported key always
    beats the file. Tolerant of `KEY = value` spacing and surrounding quotes,
    because a .env written by hand usually has both.

    The Anthropic SDK reads ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL itself, so
    putting a gateway's base URL in .env is all it takes to route through one.
    """
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value and not os.environ.get(name):
            os.environ[name] = value


def _logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactionFilter())   # no log record can bypass redaction
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"--input expects name=value, got {pair!r}")
        name, _, value = pair.partition("=")
        values[name.strip()] = value.strip()
    return values


def _register_sensitive(artifact_inputs: Any, values: dict[str, str]) -> None:
    for param in artifact_inputs:
        if param.sensitive and values.get(param.name):
            register_sensitive(param.name, values[param.name])


# ---------------------------------------------------------------- discover

@app.command()
def discover(
    goal: str = typer.Option(..., help="What to accomplish, in natural language."),
    target: str = typer.Option("http://localhost:8000/login", help="Entry point URL."),
    capability: str = typer.Option("member.open_subaccount", help="Capability spec to compile against."),
    input: list[str] = typer.Option([], help="name=value, repeatable."),
    model: str = typer.Option("claude-sonnet-5", help="Anthropic model id."),
    headed: bool = typer.Option(False, help="Watch the run in a visible browser."),
    max_steps: int = typer.Option(25),
    max_tokens: int = typer.Option(4000, help="Per-response ceiling. Lower it if a "
                                              "gateway rejects the request on credit."),
    slow_mo: int = typer.Option(0, help="Pad every browser op by N ms so a demo is watchable."),
) -> None:
    """Drive a live surface with the model, then compile the run into an artifact."""
    _logging()
    _load_env()
    from src.agent.loop import run_discovery      # imports anthropic; replay never does

    if not os.environ.get("ANTHROPIC_API_KEY"):
        typer.echo("ANTHROPIC_API_KEY is not set. Put it in .env or export it.", err=True)
        raise typer.Exit(code=2)
    gateway = os.environ.get("ANTHROPIC_BASE_URL")
    typer.echo(f"model: {model}" + (f"  via {gateway}" if gateway else "  via api.anthropic.com"))

    spec = load_spec(capability)
    inputs = _parse_inputs(input)
    spec_inputs = [p for p in spec.get("inputs", [])]
    for param in spec_inputs:
        if param.get("sensitive") and inputs.get(param["name"]):
            register_sensitive(param["name"], inputs[param["name"]])

    run_id = new_run_id("discovery")
    recorder = Recorder(run_id)
    recorder.meta(kind="discovery", goal=goal, target=target, capability=capability,
                  inputs=inputs, model=model)

    # The compiler reads the in-memory record. The disk copy is redacted, and a
    # redacted value can no longer be matched against the launch inputs, so
    # parameterisation has to happen while the real values are still in hand.
    raw: list[dict[str, Any]] = []

    def record(entry: dict[str, Any]) -> None:
        raw.append(entry)
        recorder.transcript(entry)

    surface = PlaywrightSurface(headless=not headed, trace_dir=str(recorder.dir),
                                slow_mo_ms=slow_mo)
    try:
        result = run_discovery(
            surface, goal=goal, inputs=inputs, entry_url=target,
            output_names=[o["name"] for o in spec.get("outputs", [])],
            policy=Policy.load(), record=record,
            screenshot_path=recorder.screenshot_path, model=model, max_steps=max_steps,
            max_tokens=max_tokens,
        )
        surface.screenshot(recorder.screenshot_path("final"))
    finally:
        surface.stop_tracing(recorder.trace_path)
        surface.close()

    typer.echo(f"discovery: {result.status} after {result.turns} steps — {result.reason}")
    recorder.finish(turns=result.turns, status=result.status)
    recorder.result({"status": result.status, "turns": result.turns,
                     "reason": result.reason, "outputs": result.outputs})

    if result.status != "finished":
        typer.echo(f"no artifact written; evidence in {recorder.dir}", err=True)
        raise typer.Exit(code=1)

    version = store.next_version(spec["capability_id"])
    artifact = compile_artifact(raw, spec, inputs, version=version, model=model,
                                run_id=run_id)
    path = store.save(artifact)
    typer.echo(f"compiled {artifact.capability_id} v{version} -> {path}")
    typer.echo(f"contract_hash {artifact.contract_hash[:16]}  status={artifact.status}")
    typer.echo(f"evidence: {recorder.dir}")


# ------------------------------------------------------------------ replay

@app.command()
def replay(
    artifact: str = typer.Option(..., help="Path to a capability artifact."),
    input: list[str] = typer.Option([], help="name=value, repeatable."),
    tenant: str = typer.Option("", help="Apply tenant-overrides/<tenant>.yaml."),
    unattended: bool = typer.Option(False, help="No human is watching this run."),
    headed: bool = typer.Option(False, help="Persistent visible browser; required for handoff."),
    allow_bbox: bool = typer.Option(False, help="Permit the coordinate rung (last resort)."),
    slow_mo: int = typer.Option(0, help="Pad every browser op by N ms so a demo is watchable."),
    wait_s: int = typer.Option(900, help="How long to wait for an operator."),
) -> None:
    """Execute a compiled capability. No model is involved in any decision."""
    _logging()
    from src.escalation.requests import InterventionRequest, write as write_request
    from src.escalation.state import ControlLog

    loaded = store.load(artifact)
    if tenant:
        override = load_override(tenant)
        if override is None:
            typer.echo(f"no override file for tenant {tenant!r}", err=True)
            raise typer.Exit(code=2)
        try:
            loaded = apply_override(loaded, override)
        except ContractViolation as exc:
            typer.echo(f"contract violation: {exc}", err=True)
            raise typer.Exit(code=2)

    values = _parse_inputs(input)
    _register_sensitive(loaded.inputs, values)

    run_id = new_run_id("replay")
    recorder = Recorder(run_id)
    recorder.meta(kind="replay", capability=loaded.capability_id, version=loaded.version,
                  tenant=loaded.tenant_variant, status=loaded.status,
                  attended=not unattended, inputs=values)
    control = ControlLog(recorder.path("control.jsonl"))

    surface = PlaywrightSurface(
        headless=not headed,
        user_data_dir=BROWSER_PROFILE if headed else None,
        remote_debugging_port=CDP_PORT if headed else None,
        trace_dir=str(recorder.dir),
        slow_mo_ms=slow_mo,
    )

    def on_escalate(reason: str, step_id: str, risk: str, wait: bool) -> bool:
        """Pause, hand the live session over, and wait for it back.

        With wait=False the request is filed and the run ends: a policy block
        is a decision that has already been made, so there is nothing for an
        operator to hand back to.
        """
        shot = surface.screenshot(recorder.screenshot_path(f"intervention-{step_id}"))
        request = InterventionRequest.open(
            id=run_id, run_id=run_id, capability_id=loaded.capability_id,
            version=loaded.version, goal=loaded.description, step_id=step_id,
            risk=risk, reason=reason, url=surface.page.url, screenshot_path=shot,
            cdp_url=f"http://localhost:{CDP_PORT}",
        )
        write_request(request, recorder.dir)
        typer.echo(f"\nintervention filed for {step_id}: {reason}")
        typer.echo(f"  request written to {recorder.dir / 'intervention.json'}")
        if _console_running():
            typer.echo(f"  operator console: "
                       f"http://localhost:{CONSOLE_PORT}/request/{run_id}")
        else:
            typer.echo("  no operator console is running. Start one in another "
                       "terminal to act on this:")
            typer.echo(f"    uv run python -m src.cli console --port {CONSOLE_PORT}")
        if not wait:
            return False
        control.transfer("human", reason=reason, step=step_id)
        if not headed:
            typer.echo("  NOTE: this run is headless, so there is no window for a "
                       "human to drive. Re-run with --headed for a real handoff.")
        typer.echo(f"  the browser stays open; waiting up to {wait_s}s for control back...")
        resumed = control.wait_for_agent(timeout_s=wait_s)
        typer.echo("  control returned to the agent" if resumed
                   else "  no operator response; giving up")
        return resumed

    try:
        executor = Executor(
            surface, loaded, policy=Policy.load(), attended=not unattended,
            record_step=recorder.step, screenshot_path=recorder.screenshot_path,
            on_escalate=on_escalate, run_id=run_id, allow_bbox=allow_bbox,
            control=control,
        )
        result = executor.run(values)
    finally:
        surface.stop_tracing(recorder.trace_path)
        surface.close()

    recorder.finish(status=result.status, steps=len(result.steps))
    recorder.result(result.model_dump())
    typer.echo(f"\nstatus: {result.status}   exit {result.exit_code}")
    if result.outcome:
        typer.echo(f"outcome: {result.outcome}")
    if result.outputs:
        typer.echo(f"outputs: {json.dumps(result.outputs, indent=2)}")
    if result.recoveries:
        typer.echo(f"recoveries applied: {', '.join(result.recoveries)}")
    if result.failure:
        typer.echo(f"failed at {result.failure.step_id}")
        typer.echo(f"  expected: {result.failure.expected}")
        typer.echo(f"  observed: {result.failure.observed}")
    if result.max_rung_used:
        typer.echo(f"max locator rung used: {result.max_rung_used}")
    typer.echo(f"evidence: {recorder.dir}")
    raise typer.Exit(code=result.exit_code)


# -------------------------------------------------------------------- demo

@app.command()
def demo(
    skip_discover: bool = typer.Option(
        False, help="Reuse the newest artifact instead of spending an API call."),
    headed: bool = typer.Option(False, help="Watch both runs in a visible browser."),
    slow_mo: int = typer.Option(0, help="Pad every browser op by N ms so a demo is watchable."),
    yes: bool = typer.Option(False, help="Accept the review gate without prompting."),
) -> None:
    """Run the whole thread: discover -> review -> approve -> replay.

    Shells out to the same commands the README documents and prints each one
    before running it, so what a reviewer sees is exactly what they would type.

    The review gate is a prompt rather than an implicit step: discovery emits a
    draft, and a draft may not run unattended until a human has looked at it.
    Hiding that would remove the thing the safety model is built around.
    """
    _logging()
    _load_env()
    import subprocess

    def run(*args: str, check: bool = True) -> int:
        printable = " ".join(["uv", "run", "python", "-m", "src.cli", *args])
        typer.echo(f"\n\033[1m$ {printable}\033[0m")
        code = subprocess.call([sys.executable, "-m", "src.cli", *args])
        if check and code not in (0,):
            typer.echo(f"step failed with exit {code}", err=True)
            raise typer.Exit(code=code)
        return code

    if not _target_reachable():
        typer.echo("The target application is not running. Start it first:", err=True)
        typer.echo("  uv run uvicorn mock_app.main:app --port 8000", err=True)
        raise typer.Exit(code=2)

    capability = "member.open_subaccount"
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    typer.echo("=" * 72)
    typer.echo("1/4  DISCOVER — an LLM drives the live UI and the run is compiled")
    typer.echo("=" * 72)
    if skip_discover or not have_key:
        reason = "--skip-discover" if skip_discover else "no ANTHROPIC_API_KEY"
        typer.echo(f"  skipped ({reason}); using the newest artifact on disk.")
    else:
        run("discover",
            "--goal", "Sign in, look up the member, and open a new savings "
                      "sub-account, reaching the confirmation screen",
            "--target", "http://localhost:8000/login",
            "--input", "member_id=12345", "--input", "account_type=Savings",
            "--input", "initial_deposit=250.00", "--input", "nickname=Vacation",
            *(["--headed"] if headed else []),
            *(["--slow-mo", str(slow_mo)] if slow_mo else []))

    version = store.versions(capability)[-1]
    artifact = str(store.artifact_path(capability, version))

    typer.echo("\n" + "=" * 72)
    typer.echo("2/4  REVIEW — what a human checks before this may run unattended")
    typer.echo("=" * 72)
    loaded = store.load(artifact)
    typer.echo(f"  {artifact}")
    typer.echo(f"  recorded by {loaded.recorded_by_model} "
               f"from run {loaded.discovery_run_id}")
    typer.echo(f"  contract_hash {loaded.contract_hash[:16]}  status={loaded.status}")
    typer.echo(f"\n  inputs:  " + ", ".join(
        f"{i.name}:{i.type}" + ("*" if i.sensitive else "") for i in loaded.inputs))
    typer.echo(f"  outputs: " + ", ".join(f"{o.name}:{o.type}" for o in loaded.outputs))
    typer.echo(f"  success: {describe_checkpoint(loaded.success)}")
    typer.echo(f"  declared outcomes: "
               + ", ".join(b.name for b in loaded.business_outcomes))
    typer.echo(f"  declared recoveries: "
               + ", ".join(r.name for r in loaded.recoverable))
    typer.echo("\n  steps:")
    for step in loaded.steps:
        primary = step.target.primary if step.target else None
        where = (primary.name or primary.label or primary.row_key or primary.role) \
            if primary else (step.url or "")
        rung = f"{primary.strategy}" if primary else "-"
        typer.echo(f"    {step.id:4} {step.action:9} {step.risk:13} "
                   f"{rung:15} {str(where)[:30]:32} {step.value or ''}")
    typer.echo(f"\n  (full artifact: uv run python -m src.cli show --artifact {artifact})")
    typer.echo("\n" + "=" * 72)
    typer.echo("3/4  APPROVE — the draft -> approved gate")
    typer.echo("=" * 72)
    if loaded.status == "approved":
        typer.echo(f"  {artifact} is already approved.")
    elif yes or typer.confirm(f"  Approve {artifact} for unattended replay?",
                              default=True):
        run("approve", "--artifact", artifact)
    else:
        typer.echo("  Not approved. Replay would be blocked at the first "
                   "irreversible step (exit 6).")

    typer.echo("\n" + "=" * 72)
    typer.echo("4/4  REPLAY — no model, and deliberately different inputs")
    typer.echo("=" * 72)
    typer.echo("  discovery used member 12345 / 250.00; this run uses different values,")
    typer.echo("  which is what separates a capability from a recorded macro.")
    code = run("replay", "--artifact", artifact,
               "--input", "member_id=54321", "--input", "account_type=Savings",
               "--input", "initial_deposit=99.50", "--input", "nickname=Holiday",
               *(["--headed"] if headed else []),
               *(["--slow-mo", str(slow_mo)] if slow_mo else []), check=False)

    typer.echo("\n" + "=" * 72)
    typer.echo(f"demo finished — replay exit {code}")
    typer.echo("  error taxonomy:  see README, 'Exercising the error taxonomy'")
    typer.echo("  human handoff:   uv run python scripts/escalation_demo.py")
    typer.echo("=" * 72)
    raise typer.Exit(code=code)


# ----------------------------------------------------------------- others

@app.command()
def approve(artifact: str = typer.Option(..., help="Path to the artifact to promote.")) -> None:
    """Promote a reviewed draft to approved so it may run unattended."""
    loaded = store.load(artifact)
    if not loaded.verify_contract_hash():
        typer.echo("refusing to approve: contract hash does not match contents", err=True)
        raise typer.Exit(code=2)
    loaded.status = "approved"
    path = store.save_status_change(loaded)
    typer.echo(f"approved {loaded.capability_id} v{loaded.version} -> {path}")


@app.command()
def console(port: int = typer.Option(CONSOLE_PORT), runs: str = typer.Option("runs")) -> None:
    """Serve the operator console."""
    import uvicorn
    from src.escalation import console as console_module

    if port == CDP_PORT:
        typer.echo(
            f"Refusing to serve the console on {CDP_PORT}: that port is reserved for "
            f"the paused browser's DevTools endpoint, and taking it stops --headed "
            f"runs from offering a live session at all.", err=True)
        typer.echo(f"Use the default instead:  "
                   f"uv run python -m src.cli console --port {CONSOLE_PORT}", err=True)
        raise typer.Exit(code=2)

    console_module.RUNS_DIR = Path(runs)
    typer.echo(f"operator console on http://localhost:{port}")
    uvicorn.run(console_module.app, host="127.0.0.1", port=port, log_level="warning")


@app.command()
def show(artifact: str = typer.Option(...),
         format: str = typer.Option("yaml", help="yaml or json")) -> None:
    """Print an artifact in a form a human can read."""
    loaded = store.load(artifact)
    payload = loaded.model_dump(mode="json")
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(yaml.safe_dump(payload, sort_keys=False, width=100))


@app.command()
def validate(artifact: str = typer.Option(...)) -> None:
    """Check an artifact parses and its contract hash still matches its contents."""
    loaded = store.load(artifact)
    ok = loaded.verify_contract_hash()
    typer.echo(f"{loaded.capability_id} v{loaded.version}  status={loaded.status}")
    typer.echo(f"  steps={len(loaded.steps)} inputs={len(loaded.inputs)} "
               f"outputs={len(loaded.outputs)}")
    typer.echo(f"  contract_hash {'matches' if ok else 'DOES NOT MATCH'} contents")
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def drift(capability: str = typer.Option(...), runs: str = typer.Option("runs")) -> None:
    """Compare the most recent run's locator rungs against the recorded baseline.

    A step that resolved at rung 1 when it was recorded and resolves at rung 3
    today still works, but the surface has moved underneath it. That is the
    signal to re-record, and it arrives before the capability breaks outright.
    """
    artifact = store.load_latest(capability)
    candidates = sorted(Path(runs).glob("replay-*/steps.jsonl"))
    if not candidates:
        typer.echo("no replay runs recorded yet", err=True)
        raise typer.Exit(code=1)
    latest = candidates[-1]
    traces = read_jsonl(latest)

    typer.echo(f"{capability} v{artifact.version} vs {latest.parent.name}")
    drifted = 0
    for trace in traces:
        step_id = trace.get("step_id")
        used, base = trace.get("rung_used"), artifact.baseline_rungs.get(step_id)
        if not used or not base:
            continue
        if LADDER.index(used) > LADDER.index(base):
            drifted += 1
            typer.echo(f"  DRIFT {step_id}: recorded at {base}, now resolving at {used}")
        else:
            typer.echo(f"  ok    {step_id}: {used}")
    typer.echo(f"\n{drifted} step(s) drifted" if drifted else "\nno drift")
    raise typer.Exit(code=1 if drifted else 0)


if __name__ == "__main__":
    app()
