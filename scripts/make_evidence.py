"""Regenerate everything under evidence/ by running the real CLI.

Nothing in evidence/ is hand-assembled: each directory is a run this script
performed, copied verbatim. The discovery run is not re-run here (it costs an
API call and is the one genuinely non-deterministic step) -- the most recent one
under runs/ is picked up instead.

    uv run python scripts/make_evidence.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EV = ROOT / "evidence"
ARTIFACT = "artifacts/member.open_subaccount.v1.json"
INPUTS = ["--input", "member_id=54321", "--input", "account_type=Savings",
          "--input", "initial_deposit=99.50", "--input", "nickname=Holiday"]


def inject(mode: str) -> None:
    request = urllib.request.Request(
        "http://localhost:8000/admin/inject", data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10).read()


def serve(tenant: str) -> subprocess.Popen:
    subprocess.run(["pkill", "-f", "uvicorn mock_app.main"], capture_output=True)
    time.sleep(1)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_app.main:app", "--port", "8000",
         "--log-level", "warning"],
        cwd=str(ROOT), env={**os.environ, "MOCK_TENANT": tenant},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen("http://localhost:8000/login", timeout=1)
            return process
        except Exception:
            time.sleep(0.5)
    raise SystemExit("mock app did not start")


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "src.cli", *args],
                          cwd=str(ROOT), capture_output=True, text=True)


# Never copied into evidence/. Playwright writes trace.zip itself -- it never
# passes through redact() -- and it holds full DOM snapshots and network bodies,
# so every member id and balance on screen is in it verbatim. Traces stay under
# runs/ (gitignored) for local debugging.
NEVER_COMMIT = {"trace.zip"}


def copy_run(run: Path, dest: Path, *, screenshots: bool) -> None:
    """Copy a run directory into evidence/, leaving out what redaction cannot reach.

    Screenshots are pixels, and redaction only operates on text. They are kept
    only for failed or escalated runs, whose screens were checked and stop before
    member data is shown; a success screen always shows it.
    """
    for item in run.iterdir():
        if item.name in NEVER_COMMIT:
            continue
        if item.name == "screenshots" and not screenshots:
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, dest / item.name)


def fresh(name: str) -> Path:
    dest = EV / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    return dest


def capture(name: str, result: subprocess.CompletedProcess, note: str) -> Path:
    """Copy the run directory the CLI just wrote into evidence/<name>/."""
    out = result.stdout + result.stderr
    run = next((line.split(": ", 1)[1].strip() for line in out.splitlines()
                if line.startswith("evidence: ")), None)
    dest = fresh(name)
    if run and (ROOT / run).exists():
        copy_run(ROOT / run, dest, screenshots=result.returncode not in (0, 3))
    (dest / "console-output.txt").write_text(out)
    (dest / "README.md").write_text(note)
    return dest


# -- 1. the real discovery run ----------------------------------------------

# Pair the transcript with the artifact it actually produced. Picking the newest
# discovery run instead would show one run's transcript beside another run's
# artifact the moment anyone re-runs discover.
produced_by = json.loads((ROOT / ARTIFACT).read_text()).get("discovery_run_id")
run = ROOT / "runs" / str(produced_by)
if not (produced_by and run.exists()):
    # Discovery is the one step that costs an API call and is genuinely
    # non-deterministic, and its run directory is gitignored -- so in a fresh
    # clone it is simply not there. Keep the committed evidence rather than
    # abort: everything below regenerates from what the repo ships.
    if not (EV / "discovery-run" / "transcript.jsonl").exists():
        raise SystemExit(f"{ARTIFACT} was produced by {produced_by!r}, which is not "
                         "under runs/, and no committed discovery evidence exists -- "
                         "run `cli discover` first")
    print(f"1. discovery-run  kept as committed ({produced_by} is not under runs/; "
          "re-run `cli discover` to regenerate it)")
else:
    print(f"1. discovery-run  <- {run.name}  (the run that produced {Path(ARTIFACT).name})")
    dest = fresh("discovery-run")
    copy_run(run, dest, screenshots=False)   # the final screen shows the member
    shutil.copy2(ROOT / ARTIFACT, dest / "emitted-artifact.json")

    meta = json.loads((run / "meta.json").read_text())
    res = json.loads((run / "result.json").read_text())
    (dest / "README.md").write_text(f"""# discovery-run

A genuine LLM-driven run against the live application. Model
`{meta['model']}`, called through the Anthropic SDK with tool use and prompt
caching on the system prompt and the tool definitions.

Result: **{res['status']}** after {res['turns']} steps.

## What the model was given

Only low-level primitives — `observe`, `click`, `type`, `select`, `navigate`,
`read`, `finish`, `escalate`. There is deliberately no `search_member()` or
`open_subaccount()` tool: a semantic tool would mean the flow was discovered by
whoever wrote the tool list, not by the model.

It sees the screen as an indexed accessibility-tree rendering rather than HTML.
The element ids on this surface regenerate on every request, so there is nothing
stable in the markup worth handing it.

## Files

- `transcript.jsonl` — one record per tool call: the observation it decided
  from, its reasoning where the model chose to think, the action, the guardrail
  decision, and the result.
- `emitted-artifact.json` — what the compiler produced from this transcript.
- `meta.json`, `result.json`.

Not committed: the Playwright trace and the final screenshot. Both show the member
and balance unredacted — the trace as DOM text, the screenshot as pixels — because
neither passes through `redact()`. They are written to `runs/` for local debugging.

## Two things worth looking at

**The unnamed controls.** Three steps act on form fields with no accessible name
at all. The model reached them via the label in their table row, and the
compiler recorded that as `label_adjacent` targeting rather than `role_name` —
which is why the ladder has that rung.

**The password field.** The model filled the login password box. The compiler
does not write that value into the artifact; it emits a `secrets.password`
reference that replay resolves from the environment at run time. An earlier
version inlined it, which is exactly the class of defect only a real discovery
run surfaces.

Input values are declared `sensitive`, so they appear here only masked
(`MEMB_****nn`). The transcript on disk never held them in the clear.
""")

# -- 2-5. replay against the base tenant ------------------------------------

process = serve("heritage")
try:
    inject("clear")
    print("2. replay-success")
    r = cli("replay", "--artifact", ARTIFACT, *INPUTS, "--unattended")
    capture("replay-success", r, f"""# replay-success

The approved capability replayed with **different inputs than the discovery
run** — a different member and a different opening amount. That is what
separates a parameterized capability from a recorded macro.

No `ANTHROPIC_API_KEY` was set for this run.

Exit code: {r.returncode} (success).

`steps.jsonl` records which rung of the locator ladder resolved each step. The
sub-account form controls resolve at `label_adjacent` because they carry no
accessible name — the application labels them with plain `<td>` text, as legacy
screens do.

The input values are not repeated here: they are declared `sensitive` in the
capability contract, so they appear in this directory only in masked form. The
seeded fixtures are in `mock_app/data.py`.
""")
    print("   exit", r.returncode)

    print("3. replay-business-outcome")
    inject("clear")
    r = cli("replay", "--artifact", ARTIFACT, "--input", "member_id=99999",
            "--input", "account_type=Savings", "--input", "initial_deposit=99.50",
            "--input", "nickname=Holiday", "--unattended")
    capture("replay-business-outcome", r, f"""# replay-business-outcome

A member id that is not present in the core. The application answers "No record
found", which is a **legitimate result the caller needs**, not a crash.

Exit code: {r.returncode} (`business_outcome`), with `outcome: member_not_found`
and `failure: null` in `result.json`.

Conflating this with a hard failure is the mistake the brief names. The executor
checks declared business outcomes *first*, before it concludes that a locator is
broken.
""")
    print("   exit", r.returncode)

    print("4. replay-hard-failure")
    inject("clear")
    inject("timeout")
    r = cli("replay", "--artifact", ARTIFACT, *INPUTS, "--unattended")
    capture("replay-hard-failure", r, f"""# replay-hard-failure

An injected session timeout (`POST /admin/inject {{"mode":"timeout"}}`)
invalidates the session cookie mid-flow, so the work area renders "Session
Expired" instead of the member search.

Exit code: {r.returncode} (`failed`). `result.json` carries a `FailureDetail`
naming the step, what was expected and what was actually observed — enough to
debug without re-running. `screenshots/fail-s4.png` is the richer signal: it shows
the session-expired screen, which carries no member data.

Note what the executor does *not* do: the locator ladder is exhausted and the
run stops, rather than clicking something that looks similar.
""")
    print("   exit", r.returncode)

    print("5. replay-recovered")
    inject("interstitial")
    r = cli("replay", "--artifact", ARTIFACT, *INPUTS, "--unattended")
    capture("replay-recovered", r, f"""# replay-recovered

An injected maintenance interstitial. The artifact **declares** this condition
and how to clear it, so replay dismisses the banner and carries on.

Exit code: {r.returncode} (success). `result.json` lists
`recoveries: ["maintenance_banner"]`.

This is why `recoverable` is not an exit code: it is an in-flight condition that
always resolves into one of the six statuses. The caller gets `success`, and the
fact that a recovery was needed is data on the result.
""")
    print("   exit", r.returncode)
finally:
    process.terminate()
    process.wait(timeout=10)

# -- 6. the same artifact against a second tenant ---------------------------

print("6. tenant-variant")
process = serve("community")
try:
    inject("clear")
    without = cli("replay", "--artifact", ARTIFACT, *INPUTS, "--unattended")
    inject("clear")
    with_ = cli("replay", "--artifact", ARTIFACT, "--tenant", "community",
                *INPUTS, "--unattended")
    dest = capture("tenant-variant", with_, f"""# tenant-variant

The same base artifact against a second institution running the same vendor
product, branded and configured differently — renamed controls plus one extra
acknowledgement step.

| Run | Exit | Result |
|---|---|---|
| base artifact, **no** override | {without.returncode} | fails at the renamed control |
| base artifact, **with** `--tenant community` | {with_.returncode} | succeeds |

`without-override.txt` is the first run; `console-output.txt` the second.
Nothing was re-recorded: `tenant-overrides/community.yaml` patches mechanics
only, and the merge recomputes the contract hash and would refuse anything that
changed what the capability returns.

The override was incomplete on the first attempt — two renamed buttons were
missing and the run **still passed**, because the ladder fell through to the
`ordinal` rung and the right control happened to sit in the right position. The
rung telemetry in `steps.jsonl` is what surfaced that.
""")
    (dest / "without-override.txt").write_text(without.stdout + without.stderr)
    print("   without override exit", without.returncode,
          "| with override exit", with_.returncode)
finally:
    process.terminate()
    process.wait(timeout=10)

print("\nevidence/ regenerated")
