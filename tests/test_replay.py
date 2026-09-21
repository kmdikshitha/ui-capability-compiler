"""The replay taxonomy, end to end, against the live mock app.

One test per bucket of the result contract. These are slow because they drive a
real browser through a real multi-step flow, which is the only way to find out
whether the distinction between a business outcome and a failure survives
contact with the application.
"""

from __future__ import annotations

import pytest

from src.replay.executor import Executor
from tests.conftest import inject

GOOD = {"member_id": "54321", "account_type": "Savings",
        "initial_deposit": "99.50", "nickname": "Holiday"}


def run(surface, artifact, policy, inputs, *, attended=True, status=None):
    if status:
        artifact = artifact.model_copy(deep=True)
        artifact.status = status
    return Executor(surface, artifact, policy=policy, attended=attended,
                    run_id="test").run(inputs)


def test_success_with_inputs_the_capability_was_not_recorded_with(
        surface, artifact, policy, clean_faults) -> None:
    """Proves parameterization rather than memorization: discovery used 12345."""
    result = run(surface, artifact, policy, GOOD)
    assert result.status == "success"
    assert result.exit_code == 0
    assert result.outputs["confirmation_number"].startswith("CNF-")
    assert result.outputs["new_account_number"]
    assert result.max_rung_used in ("role_name", "label_adjacent")


def test_no_such_member_is_an_answer_not_a_crash(
        surface, artifact, policy, clean_faults) -> None:
    result = run(surface, artifact, policy, {**GOOD, "member_id": "99999"})
    assert result.status == "business_outcome"
    assert result.outcome == "member_not_found"
    assert result.exit_code == 3
    assert result.failure is None


def test_permission_denied_is_also_an_answer(
        surface, artifact, policy, clean_faults) -> None:
    result = run(surface, artifact, policy, {**GOOD, "member_id": "55555"})
    assert result.status == "business_outcome"
    assert result.outcome == "permission_denied"
    assert result.exit_code == 3


@pytest.mark.parametrize("bad,reason", [
    ({"initial_deposit": "not-a-number"}, "money"),
    ({"member_id": ""}, "required"),
])
def test_bad_inputs_are_the_callers_problem(
        surface, artifact, policy, clean_faults, bad, reason) -> None:
    result = run(surface, artifact, policy, {**GOOD, **bad})
    assert result.status == "caller_error"
    assert result.exit_code == 2
    assert result.failure is not None


def test_declared_interstitial_is_recovered_and_the_run_still_succeeds(
        surface, artifact, policy, clean_faults) -> None:
    inject("interstitial")
    result = run(surface, artifact, policy, GOOD)
    assert result.status == "success"
    assert result.exit_code == 0
    assert "maintenance_banner" in result.recoveries


def test_session_timeout_is_a_hard_failure_with_a_debuggable_report(
        surface, artifact, policy, clean_faults) -> None:
    inject("timeout")
    result = run(surface, artifact, policy, GOOD)
    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.failure is not None
    assert result.failure.step_id
    assert result.failure.expected and result.failure.observed
    assert result.failure.expected != result.failure.observed


def test_irreversible_step_in_a_draft_is_blocked_when_nobody_is_watching(
        surface, artifact, policy, clean_faults) -> None:
    result = run(surface, artifact, policy, GOOD, attended=False, status="draft")
    assert result.status == "blocked_by_policy"
    assert result.exit_code == 6
    assert "irreversible" in result.failure.observed


def test_an_undeclared_dialog_escalates_rather_than_failing(
        surface, artifact, policy, clean_faults) -> None:
    """The artifact says nothing about this screen, so there is no safe
    deterministic answer and a human has to decide."""
    inject("unknown_dialog")
    result = Executor(surface, artifact, policy=policy, attended=True,
                      run_id="test", on_escalate=lambda *a: False).run(GOOD)
    assert result.status == "escalated"
    assert result.exit_code == 5
    assert "dialog" in result.failure.observed
