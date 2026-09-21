"""The replay result contract.

Six statuses, and the distinction between the first three is the whole point:

    success           the capability did its job
    business_outcome  the app gave a legitimate answer the caller needs
    failed            something broke and a human should look at the trace

"No such member" is the second, not the third. Conflating them is the mistake
the brief names, and it is a contract decision, not an implementation detail.

`recoverable` is deliberately not a status. It is an in-flight condition -- a
maintenance banner, a slow load -- that always resolves into one of the six.
Giving it an exit code would tell a caller "something happened" while leaving
them nothing to do about it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from src.artifact.models import LocatorStrategy
from src.types import ActionType

Status = Literal["success", "business_outcome", "escalated",
                 "blocked_by_policy", "caller_error", "failed"]

EXIT_CODES: dict[str, int] = {
    "success": 0,
    "failed": 1,
    "caller_error": 2,
    "business_outcome": 3,
    "escalated": 5,
    "blocked_by_policy": 6,
}


class StepTrace(BaseModel):
    step_id: str
    action: ActionType
    rung_used: LocatorStrategy | None = None
    attempted_rungs: list[LocatorStrategy] = []
    checkpoint_ok: bool | None = None
    duration_ms: int = 0
    screenshot_path: str | None = None
    note: str | None = None


class FailureDetail(BaseModel):
    step_id: str
    expected: str
    observed: str
    evidence_dir: str


class ReplayResult(BaseModel):
    status: Status
    capability_id: str
    version: int
    tenant_variant: str = "base"
    outputs: dict[str, object] | None = None
    outcome: str | None = None          # member_not_found, permission_denied, ...
    failure: FailureDetail | None = None
    steps: list[StepTrace] = []
    recoveries: list[str] = []
    max_rung_used: LocatorStrategy | None = None
    run_id: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]
