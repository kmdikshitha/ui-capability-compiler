"""Deterministic replay.

No model decides anything here. Every choice this module makes is a lookup
against the artifact: which control to find, what to type, what to assert, what
counts as a legitimate business answer and what is a failure.

That is what determinism means in this system. It does not mean nothing varies
-- the app is slow sometimes, shows a maintenance banner sometimes, and says
"no such member" sometimes. It means no model is in the loop when those things
happen, because the artifact already declared what each of them means.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from src.artifact.models import (
    BusinessOutcome, CapabilityArtifact, Checkpoint, RecoverableCondition, Step,
)
from src.guardrails.allowlist import Policy, check
from src.guardrails.redaction import register_sensitive
from src.replay.checkpoints import assert_checkpoint, describe, matches_now, observed_state
from src.replay.outcomes import FailureDetail, ReplayResult, StepTrace
from src.replay.resolver import resolve
from src.surface.playwright_driver import PlaywrightSurface
from src.types import Action, ActResult

log = logging.getLogger("replay")

TEMPLATE = re.compile(
    r"\{\{\s*(?P<ns>inputs|secrets)\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# A capability references credentials; it never carries them. Replay reads them
# from the environment, registers them for redaction so they cannot reach disk,
# and the artifact stays safe to commit and to hand to another institution.
SECRET_ENV_PREFIX = "CAPABILITY_SECRET_"

# (reason, step_id, risk, wait) -> True if a human took control and handed it back.
# wait=False files the request without blocking: the run is already ending and
# the operator is being told what happened, not asked to rescue it.
EscalationHandler = Callable[[str, str, str, bool], bool]


class CallerError(Exception):
    """The caller passed inputs the capability cannot accept."""


@dataclass
class Retried:
    """Wraps the outcome of a step that was retried.

    _run_step returns None to mean "this step is done, carry on", so a bare
    None coming back from a retry is indistinguishable from "nothing was
    retried". Wrapping it keeps a successful recovery from being read as an
    unhandled anomaly.
    """
    result: "ReplayResult | None"


def render_template(text: str | None, inputs: dict[str, Any]) -> str | None:
    if text is None:
        return None

    def substitute(match: "re.Match[str]") -> str:
        name = match.group("name")
        if match.group("ns") == "secrets":
            value = os.environ.get(f"{SECRET_ENV_PREFIX}{name.upper()}", "")
            if value:
                # Registered before it is ever typed, so no writer can leak it.
                register_sensitive(name, value)
            return value
        return str(inputs.get(name, ""))

    return TEMPLATE.sub(substitute, text)


def validate_inputs(artifact: CapabilityArtifact, inputs: dict[str, Any]) -> dict[str, Any]:
    """Check the caller's arguments against the declared contract."""
    clean: dict[str, Any] = {}
    for param in artifact.inputs:
        if param.name not in inputs or inputs[param.name] in (None, ""):
            if param.required:
                raise CallerError(f"missing required input {param.name!r}")
            clean[param.name] = ""
            continue
        raw = str(inputs[param.name])
        if param.type == "integer" and not re.fullmatch(r"-?\d+", raw):
            raise CallerError(f"input {param.name!r} must be an integer, got {raw!r}")
        if param.type == "money" and not re.fullmatch(r"-?\d{1,3}(,\d{3})*(\.\d{1,2})?|-?\d+(\.\d{1,2})?", raw):
            raise CallerError(f"input {param.name!r} must be an amount, got {raw!r}")
        if param.type == "boolean" and raw.lower() not in ("true", "false", "1", "0"):
            raise CallerError(f"input {param.name!r} must be a boolean, got {raw!r}")
        clean[param.name] = raw
    unknown = set(inputs) - {p.name for p in artifact.inputs}
    if unknown:
        raise CallerError(f"unknown input(s): {', '.join(sorted(unknown))}")
    return clean


class Executor:
    def __init__(
        self,
        surface: PlaywrightSurface,
        artifact: CapabilityArtifact,
        *,
        policy: Policy,
        attended: bool = True,
        record_step: Callable[[dict[str, Any]], None] | None = None,
        screenshot_path: Callable[[str], str] | None = None,
        on_escalate: EscalationHandler | None = None,
        run_id: str | None = None,
        allow_bbox: bool = False,
        control: Any | None = None,
    ) -> None:
        self.surface = surface
        self.artifact = artifact
        self.policy = policy
        self.attended = attended
        self.record_step = record_step or (lambda _: None)
        self.screenshot_path = screenshot_path or (lambda label: f"/tmp/{label}.png")
        self.on_escalate = on_escalate
        self.run_id = run_id
        self.allow_bbox = allow_bbox
        self.control = control
        self.traces: list[StepTrace] = []
        self.recoveries: list[str] = []
        self._recovery_counts: dict[str, int] = {}

    # -- entry point -------------------------------------------------------

    def run(self, inputs: dict[str, Any]) -> ReplayResult:
        try:
            clean = validate_inputs(self.artifact, inputs)
        except CallerError as exc:
            return self._result("caller_error", failure=FailureDetail(
                step_id="-", expected="inputs matching the declared contract",
                observed=str(exc), evidence_dir=self.run_id or ""))

        for precondition in self.artifact.preconditions:
            if not assert_checkpoint(self.surface, precondition):
                return self._result("failed", failure=FailureDetail(
                    step_id="precondition", expected=describe(precondition),
                    observed=observed_state(self.surface), evidence_dir=self.run_id or ""))

        for step in self.artifact.steps:
            outcome = self._run_step(step, clean)
            if outcome is not None:
                return outcome

        if not assert_checkpoint(self.surface, self.artifact.success):
            classified = self._classify(self.artifact.success, "success")
            if classified is not None:
                return classified
            return self._result("failed", failure=FailureDetail(
                step_id="success", expected=describe(self.artifact.success),
                observed=observed_state(self.surface), evidence_dir=self.run_id or ""))

        return self._result("success", outputs=self._extract_outputs())

    # -- steps -------------------------------------------------------------

    def _run_step(self, step: Step, inputs: dict[str, Any],
                  attempt: int = 1) -> ReplayResult | None:
        """Returns None to continue, or a terminal ReplayResult."""
        started = time.monotonic()
        # Checked before every action, not once at startup. The whole risk in a
        # handoff is the window where both parties think they hold the wheel.
        if self.control is not None:
            self.control.assert_control("agent")

        # A modal is checked for before acting, not after something fails.
        # Reaching past one is worse than stopping at it: the locator ladder
        # will happily resolve "the first textbox" to a field inside the dialog
        # and type a member id into it, and every later step then operates on a
        # screen nobody intended.
        blocker = self._undeclared_dialog()
        if blocker is not None:
            escalated = self._escalate_dialog(step, inputs, attempt, blocker)
            if escalated is not None:
                return escalated.result

        value = render_template(step.value, inputs)
        url = render_template(step.url, inputs)

        action = Action(type=step.action, value=value, url=url)
        decision = check(action, current_url=self.surface.page.url, policy=self.policy,
                         risk=step.risk, artifact_status=self.artifact.status,
                         attended=self.attended)
        if not decision.allowed:
            if decision.outcome == "needs_intervention" and self.on_escalate:
                # The rule blocked the action; a human is still told about it,
                # so the run can be taken forward deliberately rather than
                # silently dropped.
                self.on_escalate(decision.reason, step.id, step.risk, False)
            self._trace(step, None, [], None, started, note=decision.reason)
            return self._result("blocked_by_policy", failure=FailureDetail(
                step_id=step.id, expected="an action permitted by policy",
                observed=decision.reason, evidence_dir=self.run_id or ""))

        if step.action == "navigate":
            result = self.surface.act(Action(type="navigate", url=url))
            rung, attempted = None, []
        else:
            if step.target is None:
                return self._fail(step, "a target to resolve", "step has no target", started)
            resolved = resolve(self.surface, step.target, self.allow_bbox)
            rung, attempted = resolved.rung, resolved.attempted
            if not resolved.ok:
                # Exhausting the ladder is never a guess. Before calling it a
                # failure, check whether the app is telling us something the
                # artifact already knows how to interpret.
                primary = step.target.primary
                wanted = primary.name or primary.label or primary.row_key or primary.role
                return self._handle_anomaly(
                    step, inputs, attempt,
                    expected=f'{primary.role} "{wanted}" (via {primary.strategy})',
                    observed=f"ladder exhausted after {attempted}; "
                             f"{observed_state(self.surface)}",
                    started=started, rung=rung, attempted=attempted)
            if resolved.bbox:
                # Recorded coordinates. Reaching this rung means every
                # structural rung failed; it is logged loudly because the
                # capability is one layout change from breaking outright.
                log.warning("step %s resolved by bbox; re-record this capability", step.id)
                x, y, w, h = resolved.bbox
                self.surface.page.mouse.click(x + w / 2, y + h / 2)
                result = ActResult(ok=True)
            else:
                result = self.surface.act_on_locator(resolved.locator, step.action, value)

        if result is not None and not result.ok:
            return self._handle_anomaly(
                step, inputs, attempt, expected=f"{step.action} to succeed",
                observed=result.error or "action failed", started=started,
                rung=rung, attempted=attempted)

        checkpoint_ok = None
        if step.checkpoint is not None:
            checkpoint_ok = assert_checkpoint(self.surface, step.checkpoint)
            if not checkpoint_ok:
                return self._handle_anomaly(
                    step, inputs, attempt, expected=describe(step.checkpoint),
                    observed=observed_state(self.surface), started=started,
                    rung=rung, attempted=attempted, checkpoint_ok=False)

        self._trace(step, rung, attempted, checkpoint_ok, started)
        return None

    def _handle_anomaly(self, step: Step, inputs: dict[str, Any], attempt: int, *,
                        expected: str, observed: str, started: float,
                        rung=None, attempted=None,
                        checkpoint_ok: bool | None = None) -> ReplayResult:
        """The classification order, applied in one place.

        1. a declared business outcome -- checked first, always
        2. a declared recoverable condition -- recover and retry, bounded
        3. an unknown blocking dialog -- a human decides, not us
        4. anything else -- a hard failure with expected vs observed

        Order matters. Checking business outcomes first is what keeps "no such
        member" from being reported as a broken locator.
        """
        classified = self._classify(None, step.id)
        if classified is not None:
            return classified

        retried = self._try_recovery(step, inputs, attempt)
        if retried is not None:
            return retried.result

        escalated = self._escalate_if_blocked(step, inputs, attempt)
        if escalated is not None:
            return escalated.result

        return self._fail(step, expected, observed, started, rung=rung,
                          attempted=attempted, checkpoint_ok=checkpoint_ok)

    def _undeclared_dialog(self) -> str | None:
        """An open dialog the artifact has no answer for.

        A dialog that a declared business outcome or recovery already covers is
        not undeclared -- that is the artifact doing its job, and it should be
        handled by the normal classification order rather than sent to a human.
        """
        name = self.surface.blocking_dialog()
        if name is None:
            return None
        declared = [o.detect for o in self.artifact.business_outcomes]
        declared += [r.detect for r in self.artifact.recoverable]
        for detect in declared:
            if matches_now(self.surface, detect):
                return None
        return name

    def _escalate_dialog(self, step: Step, inputs: dict[str, Any], attempt: int,
                         name: str) -> Retried | None:
        reason = f"unexpected dialog on screen: {name!r}"
        shot = self.surface.screenshot(self.screenshot_path(f"escalate-{step.id}"))
        log.warning("escalating at %s: %s", step.id, reason)
        if self.on_escalate and self.on_escalate(reason, step.id, step.risk, True):
            self.surface.observe()
            if self._undeclared_dialog() is not None:
                return Retried(self._result("escalated", failure=FailureDetail(
                    step_id=step.id, expected="the dialog to be resolved",
                    observed=f"{reason} is still on screen after the handoff",
                    evidence_dir=self.run_id or "")))
            return Retried(self._run_step(step, inputs, attempt + 1))
        return Retried(self._result("escalated", failure=FailureDetail(
            step_id=step.id, expected="a screen the capability knows how to handle",
            observed=reason, evidence_dir=self.run_id or "")))

    def _escalate_if_blocked(self, step: Step, inputs: dict[str, Any],
                             attempt: int) -> Retried | None:
        """An undeclared dialog is a decision, not a defect.

        The artifact says nothing about this screen, so there is no safe
        deterministic answer. Rather than guess, hand the live session to a
        human and let them decide; if they hand it back, re-observe from
        scratch and carry on.
        """
        try:
            obs = self.surface.observe()
        except Exception:
            return None
        blocking = next((e for e in obs.elements if e.role in ("dialog", "alert")), None)
        if blocking is None:
            return None

        reason = f"unexpected {blocking.role} on screen: {blocking.name!r}"
        shot = self.surface.screenshot(self.screenshot_path(f"escalate-{step.id}"))
        log.warning("escalating at %s: %s", step.id, reason)
        if self.on_escalate and self.on_escalate(reason, step.id, step.risk, True):
            # Never assume the page is where we left it: the human may have
            # navigated anywhere. Re-observe and retry the step from scratch.
            self.surface.observe()
            return Retried(self._run_step(step, inputs, attempt + 1))

        return Retried(self._result("escalated", failure=FailureDetail(
            step_id=step.id, expected="a screen the capability knows how to handle",
            observed=reason, evidence_dir=self.run_id or "")))

    # -- anomaly classification -------------------------------------------

    def _classify(self, checkpoint: Checkpoint | None, step_id: str) -> ReplayResult | None:
        """Declared business outcomes are checked first, always.

        A "no such member" screen reaching this function must come back as an
        answer with an exit code the caller can branch on, not as a failure
        report about a control that could not be found.
        """
        for outcome in self.artifact.business_outcomes:
            if matches_now(self.surface, outcome.detect):
                log.info("business outcome %s at %s", outcome.name, step_id)
                return self._result("business_outcome", outcome=outcome.name,
                                    outputs=dict(outcome.returns))
        return None

    def _try_recovery(self, step: Step, inputs: dict[str, Any],
                      attempt: int) -> Retried | None:
        """Run a declared recovery and retry the step, bounded by max_attempts.

        The recovery is data in the artifact, not code here: dismissing a
        maintenance banner is a property of the application, and the executor
        should not have opinions about it.
        """
        for condition in self.artifact.recoverable:
            if not matches_now(self.surface, condition.detect):
                continue
            used = self._recovery_counts.get(condition.name, 0)
            if used >= condition.max_attempts or attempt > condition.max_attempts:
                return None
            self._recovery_counts[condition.name] = used + 1
            self.recoveries.append(condition.name)
            log.info("recovering from %s (attempt %d)", condition.name, used + 1)
            for recovery_step in condition.recover:
                resolved = resolve(self.surface, recovery_step.target) \
                    if recovery_step.target else None
                if resolved is None or not resolved.ok:
                    return None
                self.surface.act_on_locator(resolved.locator, recovery_step.action,
                                            render_template(recovery_step.value, inputs))
            return Retried(self._run_step(step, inputs, attempt + 1))
        return None

    # -- outputs -----------------------------------------------------------

    def _extract_outputs(self) -> dict[str, Any]:
        """Re-read every declared output off the screen.

        Deliberately not taken from the transcript: the point of a capability
        is that the caller gets what the application says now, not what it said
        the day the flow was recorded.
        """
        from src.artifact.models import TargetSpec
        values: dict[str, Any] = {}
        for output in self.artifact.outputs:
            spec = TargetSpec(primary=output.extract, rationale="output extraction")
            resolved = resolve(self.surface, spec)
            if not resolved.ok or resolved.locator is None:
                values[output.name] = None
                continue
            result = self.surface.act_on_locator(resolved.locator, "read", None)
            values[output.name] = result.read_value
        return values

    # -- bookkeeping -------------------------------------------------------

    def _trace(self, step: Step, rung, attempted, checkpoint_ok, started,
               note: str | None = None) -> None:
        trace = StepTrace(
            step_id=step.id, action=step.action, rung_used=rung,
            attempted_rungs=attempted or [], checkpoint_ok=checkpoint_ok,
            duration_ms=int((time.monotonic() - started) * 1000), note=note,
        )
        self.traces.append(trace)
        self.record_step(trace.model_dump())

    def _fail(self, step: Step, expected: str, observed: str, started,
              rung=None, attempted=None, checkpoint_ok=None) -> ReplayResult:
        shot = self.surface.screenshot(self.screenshot_path(f"fail-{step.id}"))
        trace = StepTrace(
            step_id=step.id, action=step.action, rung_used=rung,
            attempted_rungs=attempted or [], checkpoint_ok=checkpoint_ok,
            duration_ms=int((time.monotonic() - started) * 1000), screenshot_path=shot,
        )
        self.traces.append(trace)
        self.record_step(trace.model_dump())
        return self._result("failed", failure=FailureDetail(
            step_id=step.id, expected=expected, observed=observed,
            evidence_dir=self.run_id or ""))

    def _result(self, status: str, **fields: Any) -> ReplayResult:
        rungs = [t.rung_used for t in self.traces if t.rung_used]
        from src.artifact.models import LADDER
        worst = max(rungs, key=LADDER.index) if rungs else None
        return ReplayResult(
            status=status, capability_id=self.artifact.capability_id,
            version=self.artifact.version, tenant_variant=self.artifact.tenant_variant,
            steps=self.traces, recoveries=self.recoveries, max_rung_used=worst,
            run_id=self.run_id, **fields,
        )
