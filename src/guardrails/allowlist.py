"""The one gate every action passes through.

check() is called before every action in discovery and in replay. The shared
code path is the design point. Discovery is the more dangerous of the two --
an LLM is exploring freely -- so it gets the same gate rather than a laxer one.

The risk policy differs by path, not the allowlist:
    discovery may explore; replay may only execute what was reviewed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel

from src.guardrails.risk import Risk
from src.types import Action

log = logging.getLogger("guardrails")

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "allowlist.yaml"

Outcome = Literal["allow", "blocked_by_policy", "needs_intervention"]


class Policy(BaseModel):
    origins: list[str] = []
    paths: list[str] = []
    actions: list[str] = []
    deny_paths: list[str] = []

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        return cls(**yaml.safe_load(Path(path or DEFAULT_POLICY_PATH).read_text()))


class Decision(BaseModel):
    allowed: bool
    outcome: Outcome
    reason: str


def _allow(reason: str) -> Decision:
    return Decision(allowed=True, outcome="allow", reason=reason)


def _deny(outcome: Outcome, reason: str) -> Decision:
    # Denials are logged, never silently skipped.
    log.warning("guardrail %s: %s", outcome, reason)
    return Decision(allowed=False, outcome=outcome, reason=reason)


def url_allowed(url: str, policy: Policy) -> tuple[bool, str]:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in policy.origins:
        return False, f"origin {origin!r} is not on the allowlist"
    path = parsed.path or "/"
    if any(re.search(p, path) for p in policy.deny_paths):
        return False, f"path {path!r} matches a deny rule"
    if not any(re.match(p, path) for p in policy.paths):
        return False, f"path {path!r} is not on the allowlist"
    return True, "ok"


def check(
    action: Action,
    *,
    current_url: str,
    policy: Policy,
    risk: Risk = "safe",
    artifact_status: str = "approved",
    attended: bool = True,
) -> Decision:
    """Allow, block, or demand a human. Called before every action."""
    if action.type not in policy.actions:
        return _deny("blocked_by_policy", f"action type {action.type!r} is not permitted")

    target = action.url if action.type == "navigate" else current_url
    ok, why = url_allowed(target or "", policy)
    if not ok:
        return _deny("blocked_by_policy", why)

    if risk == "irreversible":
        if artifact_status != "approved" and not attended:
            return _deny(
                "needs_intervention",
                "irreversible step in a draft capability with nobody attending",
            )
        return _allow(
            "irreversible step permitted: "
            + ("capability is approved" if artifact_status == "approved" else "run is attended")
        )
    return _allow(f"{risk} action within the allowlist")
