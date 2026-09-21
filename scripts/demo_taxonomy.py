"""Walk the result taxonomy in one pass, for a demo or a quick manual check.

Same artifact, same command shape, six different situations — and what changes
is the exit code and the structured result, which is what a calling agent
actually consumes.

    uv run python scripts/demo_taxonomy.py
    uv run python scripts/demo_taxonomy.py --pause     # wait for a keypress between cases
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = "artifacts/member.open_subaccount.v1.json"

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, BLUE = "\033[32m", "\033[33m", "\033[31m", "\033[34m"

COLOUR = {0: GREEN, 3: BLUE, 2: YELLOW, 5: YELLOW, 6: YELLOW, 1: RED}

CASES = [
    ("baseline — the happy path", None, {}, 0,
     "outputs read off the confirmation screen"),
    ("member that does not exist", None, {"member_id": "99999"}, 3,
     "a legitimate answer, not a crash — note exit 3, and failure: null"),
    ("member the operator may not service", None, {"member_id": "55555"}, 3,
     "the other declared business outcome"),
    ("deposit that is not an amount", None, {"initial_deposit": "abc"}, 2,
     "rejected against the contract before a browser even opens"),
    ("maintenance banner", "interstitial", {}, 0,
     "declared in the artifact, so it is dismissed and the run still succeeds"),
    ("session expires mid-flow", "timeout", {}, 1,
     "undeclared — stops with expected vs observed rather than guessing"),
    ("a dialog the artifact never saw", "unknown_dialog", {}, 5,
     "no safe deterministic action, so it asks for a human"),
]


def inject(mode: str) -> None:
    request = urllib.request.Request(
        "http://localhost:8000/admin/inject", data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10).read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pause", action="store_true",
                        help="wait for Enter between cases")
    args = parser.parse_args()

    try:
        urllib.request.urlopen("http://localhost:8000/login", timeout=2)
    except Exception:
        print("The target application is not running:\n"
              "  uv run uvicorn mock_app.main:app --port 8000", file=sys.stderr)
        return 2

    failures = []
    for title, fault, overrides, expected, note in CASES:
        inputs = {"member_id": "54321", "account_type": "Savings",
                  "initial_deposit": "99.50", "nickname": "Holiday", **overrides}
        inject("clear")
        if fault:
            inject(fault)

        print(f"\n{BOLD}{'─' * 74}{RESET}")
        print(f"{BOLD}{title}{RESET}")
        if fault:
            print(f"{DIM}  armed: POST /admin/inject {{\"mode\":\"{fault}\"}}{RESET}")
        flags = " ".join(f"--input {k}={v}" for k, v in inputs.items())
        print(f"{DIM}  $ cli replay --artifact {ARTIFACT} {flags} --unattended{RESET}")

        result = subprocess.run(
            [sys.executable, "-m", "src.cli", "replay", "--artifact", ARTIFACT,
             *sum((["--input", f"{k}={v}"] for k, v in inputs.items()), []),
             "--unattended", "--wait-s", "3"],
            cwd=str(ROOT), capture_output=True, text=True)

        for line in (result.stdout + result.stderr).splitlines():
            if line.startswith(("status:", "outcome:", "outputs:", "recoveries",
                                "failed at", "  expected:", "  observed:", '  "')):
                print("  " + line.strip()[:150])

        ok = result.returncode == expected
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        tone = COLOUR.get(result.returncode, "")
        print(f"  {mark} exit {tone}{result.returncode}{RESET} "
              f"(expected {expected}) — {note}")
        if not ok:
            failures.append(title)
        if args.pause:
            input(f"{DIM}  [Enter]{RESET}")

    inject("clear")
    print(f"\n{BOLD}{'─' * 74}{RESET}")
    if failures:
        print(f"{RED}{len(failures)} case(s) did not match: {', '.join(failures)}{RESET}")
        return 1
    print(f"{GREEN}all {len(CASES)} cases behaved as declared{RESET}")
    print(f"{DIM}the handoff is separate: uv run python scripts/escalation_demo.py{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
