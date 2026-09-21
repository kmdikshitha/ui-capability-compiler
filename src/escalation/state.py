"""The controller token and its transitions.

Exactly one party may act on the session at a time, and which one is a fact on
disk rather than an assumption in a variable. assert_control() is called before
every action, not once at startup: the whole risk in a handoff is the window
where both sides think they hold the wheel.

control.jsonl is the transport, not a log. The console appends the resume
record; the CLI polls for it. Because the audit trail is the channel, it cannot
drift from what actually happened -- there is no separate signalling path that
could disagree with the record.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

Controller = Literal["agent", "human", "none"]


class ControlViolation(Exception):
    """Something tried to act while another party held control."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ControlLog:
    """Append-only control transfers for one run."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def controller(self) -> Controller:
        records = self.records()
        return records[-1]["to"] if records else "agent"

    def transfer(self, to: Controller, *, reason: str, step: str | None = None,
                 by: str = "system") -> None:
        record = {"ts": _now(), "from": self.controller(), "to": to,
                  "reason": reason, "step": step, "by": by}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def assert_control(self, expected: Controller = "agent") -> None:
        current = self.controller()
        if current != expected:
            raise ControlViolation(
                f"{expected} tried to act while {current} holds control of the session")

    def wait_for_agent(self, timeout_s: int = 900, poll_s: float = 1.0) -> bool:
        """Block until a human hands control back, or the wait times out.

        Polling a file is the entire IPC mechanism. It is enough here: one run,
        one operator, one file. Anything queue-shaped would be infrastructure
        this project is explicitly not meant to build.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.controller() == "agent":
                return True
            if self.controller() == "none":
                return False          # aborted by the operator
            time.sleep(poll_s)
        return False
