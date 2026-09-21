# escalation

The full handoff cycle: **escalate → human takes the live session → resume →
complete**, on one browser session. Regenerate with
`uv run python scripts/escalation_demo.py`.

An undeclared "Supervisor Override Required" dialog is injected mid-flow
(`POST /admin/inject {"mode":"unknown_dialog"}`). The artifact says nothing about
this screen, so there is no safe deterministic action — the capability stops
rather than guessing.

## What happened

1. Replay detects a blocking dialog it has no declared answer for, **before**
   attempting the step. It files `intervention.json` and appends
   `agent -> human` to `control.jsonl`. The CLI stops sending commands. The
   browser stays open.
2. A **separate process** attaches to that same live session over CDP
   (`connect_over_cdp`) and gets the same tab, cookies and scroll position. Not
   a fresh browser.
3. It enters the override code the agent could not supply, clearing the dialog.
4. It appends `human -> agent` to `control.jsonl`. That append **is** the resume
   signal — the paused run is polling that same file.
5. The agent re-observes from scratch, confirms the blocker is gone, retries the
   step and finishes the run: `success`, exit 0, both outputs returned.

## Files

- `control.jsonl` — the control transfers. This file is the transport, not just
  a log; because the audit trail is the channel, the two cannot disagree.
- `intervention.json` — what the operator was handed. Note the **absence** of
  the input parameter values: a member id is regulated data, and an operator who
  needs it can read it off the session they are taking control of.
- `steps.jsonl` — the step trace, including the retry after resume.
- `result.json`, `screenshots/`.

## Why this shape

`assert_control("agent")` runs before every action, not once at startup — the
whole risk in a handoff is the window where both parties believe they hold the
wheel.

On resume the executor never assumes the page is where it left it. The human may
have navigated anywhere, so it re-observes and re-checks the blocking condition
before continuing.

The stand-in operator here is a thread so the cycle runs unattended. In real use
it is a person at `uv run python -m src.cli console`, and nothing on the
automation side of the seam knows the difference.
