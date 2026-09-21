"""The capability artifact.

A capability has a contract and it has mechanics.

    contract  = inputs + outputs + success condition + declared business outcomes
    mechanics = steps, locators, recoveries

The contract is what a calling agent depends on. The mechanics are how this
particular surface happens to be driven today. contract_hash covers the contract
and nothing else, which is what makes a tenant override safe: an institution may
change how a control is found, never what the capability returns.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel

from src.guardrails.risk import Risk
from src.types import ActionType, Role

LocatorStrategy = Literal["role_name", "label_adjacent", "table_anchor", "ordinal", "bbox"]

# The ladder, best first. Rung order is policy, not preference: every rung below
# role_name trades a little robustness for a little reach.
LADDER: tuple[LocatorStrategy, ...] = (
    "role_name", "label_adjacent", "table_anchor", "ordinal", "bbox",
)


class Locator(BaseModel):
    strategy: LocatorStrategy
    role: Role | None = None
    name: str | None = None
    label: str | None = None           # label_adjacent: the row's leading cell
    header: str | None = None          # table_anchor: column header
    row_key: str | None = None         # table_anchor: value identifying the row
    index: int | None = None           # ordinal: nth of this role in the frame
    bbox: tuple[int, int, int, int] | None = None
    frame_path: list[str] = []


class TargetSpec(BaseModel):
    primary: Locator
    fallbacks: list[Locator] = []
    rationale: str                     # why this targeting is robust


class Checkpoint(BaseModel):
    type: Literal["element_visible", "text_present", "url_matches"]
    role: Role | None = None
    name: str | None = None
    text: str | None = None
    pattern: str | None = None
    timeout_ms: int = 8000


class Step(BaseModel):
    id: str                            # s1, s2, ...
    action: ActionType
    target: TargetSpec | None = None
    value: str | None = None           # "{{ inputs.member_id }}" or a literal
    url: str | None = None             # navigate only; may be templated
    risk: Risk = "safe"
    checkpoint: Checkpoint | None = None
    note: str | None = None


class InputParam(BaseModel):
    name: str
    type: Literal["string", "integer", "money", "date", "boolean"]
    required: bool = True
    sensitive: bool = False            # drives redaction
    description: str | None = None


class OutputParam(BaseModel):
    name: str
    type: Literal["string", "integer", "money", "date", "boolean"]
    extract: Locator
    description: str | None = None


class BusinessOutcome(BaseModel):
    """A legitimate answer the caller needs, not a crash."""
    name: str                          # member_not_found, permission_denied
    detect: Checkpoint
    returns: dict[str, str] = {}
    description: str | None = None


class RecoverableCondition(BaseModel):
    """An in-flight condition with a declared, data-driven recovery."""
    name: str                          # maintenance_banner, transient_slow
    detect: Checkpoint
    recover: list[Step] = []
    max_attempts: int = 2
    description: str | None = None


class CapabilityArtifact(BaseModel):
    schema_version: int = 1
    capability_id: str                 # member.open_subaccount
    version: int
    status: Literal["draft", "approved"] = "draft"
    contract_hash: str = ""
    description: str
    target_app: str
    tenant_variant: str = "base"
    entry_url_pattern: str             # /members/:id, canonicalized
    preconditions: list[Checkpoint] = []
    inputs: list[InputParam]
    outputs: list[OutputParam]
    steps: list[Step]
    success: Checkpoint
    business_outcomes: list[BusinessOutcome] = []
    recoverable: list[RecoverableCondition] = []
    baseline_rungs: dict[str, LocatorStrategy] = {}   # step id -> rung at record time
    recorded_at: str
    recorded_by_model: str
    discovery_run_id: str | None = None

    # -- contract ----------------------------------------------------------

    def contract(self) -> dict[str, Any]:
        """The subset a calling agent depends on."""
        return {
            "inputs": [i.model_dump(mode="json") for i in self.inputs],
            "outputs": [o.model_dump(mode="json") for o in self.outputs],
            "success": self.success.model_dump(mode="json"),
            "business_outcomes": [b.model_dump(mode="json") for b in self.business_outcomes],
        }

    def compute_contract_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.contract()).encode("utf-8")).hexdigest()

    def with_contract_hash(self) -> "CapabilityArtifact":
        self.contract_hash = self.compute_contract_hash()
        return self

    def verify_contract_hash(self) -> bool:
        return self.contract_hash == self.compute_contract_hash()


def canonical_json(obj: Any) -> str:
    """Byte-stable JSON. The contract hash depends on it."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class ContractViolation(Exception):
    """Raised when a tenant override would change what a capability returns."""
