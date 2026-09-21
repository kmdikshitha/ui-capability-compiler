"""Risk classification.

A rule table over the action type and the control's accessible name. It is
deliberately crude and deliberately pessimistic: a click whose name reads like
a commitment is treated as irreversible. Being wrong in that direction costs an
intervention request; being wrong in the other direction posts a transaction.
"""

from __future__ import annotations

import re
from typing import Literal

Risk = Literal["safe", "reversible", "irreversible"]

RISK_ORDER: dict[str, int] = {"safe": 0, "reversible": 1, "irreversible": 2}

# Verbs that commit something in a banking back office.
COMMIT_WORDS = re.compile(
    r"(?i)\b(confirm|submit|transfer|open|create|delete|post|approve|authorize|"
    r"remove|close|send|pay|withdraw|acknowledge)\b"
)


def classify(action_type: str, name: str | None = None) -> Risk:
    if action_type in ("navigate", "read"):
        return "safe"
    if action_type in ("type", "select"):
        return "reversible"
    if action_type == "click":
        return "irreversible" if COMMIT_WORDS.search(name or "") else "reversible"
    return "reversible"
