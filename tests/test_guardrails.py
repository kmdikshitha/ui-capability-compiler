"""The allowlist, the risk table, and the one gate both paths share."""

from __future__ import annotations

import pytest

from src.guardrails.allowlist import check, url_allowed
from src.guardrails.risk import classify
from src.types import Action

MEMBER = "http://localhost:8000/members/12345"


@pytest.mark.parametrize("action_type,name,expected", [
    ("navigate", None, "safe"),
    ("read", "Available Balance", "safe"),
    ("type", "Initial Deposit", "reversible"),
    ("select", "Account Type", "reversible"),
    ("click", "Search", "reversible"),
    ("click", "Open Sub-Account", "irreversible"),
    ("click", "Create Sub-Account", "irreversible"),
    ("click", "Confirm and Post", "irreversible"),
])
def test_risk_classification(action_type, name, expected) -> None:
    assert classify(action_type, name) == expected


def test_off_origin_is_denied(policy) -> None:
    decision = check(Action(type="navigate", url="http://evil.test/members/1"),
                     current_url=MEMBER, policy=policy)
    assert decision.outcome == "blocked_by_policy"
    assert "origin" in decision.reason


def test_deny_path_beats_allow_path(policy) -> None:
    """Fault injection is an endpoint the agent must never reach."""
    decision = check(Action(type="navigate", url="http://localhost:8000/admin/inject"),
                     current_url=MEMBER, policy=policy)
    assert decision.outcome == "blocked_by_policy"
    ok, why = url_allowed("http://localhost:8000/admin/inject", policy)
    assert not ok and "deny" in why


def test_unlisted_action_type_is_denied(policy) -> None:
    policy.actions = ["read"]
    decision = check(Action(type="click", ref="e1"), current_url=MEMBER, policy=policy)
    assert decision.outcome == "blocked_by_policy"


def test_irreversible_draft_unattended_is_blocked(policy) -> None:
    decision = check(Action(type="click", ref="e1"), current_url=MEMBER, policy=policy,
                     risk="irreversible", artifact_status="draft", attended=False)
    assert decision.outcome == "needs_intervention"
    assert not decision.allowed


def test_irreversible_is_allowed_once_approved(policy) -> None:
    decision = check(Action(type="click", ref="e1"), current_url=MEMBER, policy=policy,
                     risk="irreversible", artifact_status="approved", attended=False)
    assert decision.allowed


def test_discovery_may_explore(policy) -> None:
    """Discovery is attended and explicitly a sandbox; replay is not."""
    decision = check(Action(type="click", ref="e1"), current_url=MEMBER, policy=policy,
                     risk="irreversible", artifact_status="draft", attended=True)
    assert decision.allowed
