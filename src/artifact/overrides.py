"""Tenant overrides.

Hundreds of institutions run the same vendor product, branded and configured
differently. Re-recording a capability per tenant does not scale and, worse,
lets the same capability quietly mean different things at different tenants.

So: one base artifact, plus a small YAML patch per tenant that may touch
mechanics only. After merging, the contract hash is recomputed and compared. If
it moved, the override changed what the capability returns and it is rejected.
Without that rule one tenant's override can make get_balance read the wrong
column and every calling agent is none the wiser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.artifact.models import (
    CapabilityArtifact, Checkpoint, ContractViolation, Locator, Step,
)

OVERRIDE_DIR = Path("tenant-overrides")


def override_path(tenant: str, directory: Path | str = OVERRIDE_DIR) -> Path:
    return Path(directory) / f"{tenant}.yaml"


def load_override(tenant: str, directory: Path | str = OVERRIDE_DIR) -> dict[str, Any] | None:
    path = override_path(tenant, directory)
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def apply_override(artifact: CapabilityArtifact, override: dict[str, Any]) -> CapabilityArtifact:
    """Merge a tenant patch into a base artifact.

    Supported patches, all mechanics:
      steps:         {step_id: {target: {...}, value: ..., url: ..., risk: ...}}
      insert_steps:  [{after: step_id, step: {...}}]
      recoverable:   additional declared recoveries

    An `outputs` patch is parsed but always rejected by the contract-hash check;
    see the comment at that branch.
    """
    before = artifact.compute_contract_hash()
    patched = artifact.model_copy(deep=True)

    for step_id, patch in (override.get("steps") or {}).items():
        step = _find_step(patched, step_id)
        if step is None:
            raise ValueError(f"override targets unknown step {step_id!r}")
        if "target" in patch:
            _patch_target(step, patch["target"])
        if "value" in patch:
            step.value = patch["value"]
        if "url" in patch:
            step.url = patch["url"]
        if "risk" in patch:
            step.risk = patch["risk"]
        if "checkpoint" in patch:
            step.checkpoint = Checkpoint(**patch["checkpoint"])

    for insertion in (override.get("insert_steps") or []):
        step = Step(**insertion["step"])
        after = insertion.get("after")
        index = len(patched.steps)
        if after is not None:
            match = _find_step(patched, after)
            if match is None:
                raise ValueError(f"override inserts after unknown step {after!r}")
            index = patched.steps.index(match) + 1
        patched.steps.insert(index, step)

    # An output's extract locator looks exactly like mechanics -- it is a
    # Locator, same as any step target -- but it lives in the contract, because
    # it decides what the caller gets back. Patching it is accepted here and
    # then refused by the hash check below, on purpose: that is the single case
    # where "just a selector change" would silently change a capability's
    # meaning for every agent calling it. A tenant that genuinely extracts a
    # different field needs a new version, not an override.
    for name, patch in (override.get("outputs") or {}).items():
        output = next((o for o in patched.outputs if o.name == name), None)
        if output is None:
            raise ValueError(f"override targets unknown output {name!r}")
        if "extract" in patch:
            output.extract = Locator(**patch["extract"])

    if override.get("recoverable"):
        from src.artifact.models import RecoverableCondition
        patched.recoverable = patched.recoverable + [
            RecoverableCondition(**r) for r in override["recoverable"]
        ]

    patched.tenant_variant = override.get("tenant_variant", patched.tenant_variant)

    after_hash = patched.compute_contract_hash()
    if after_hash != before:
        raise ContractViolation(
            f"tenant override changed the capability contract "
            f"({before[:12]} -> {after_hash[:12]}); overrides may patch mechanics only"
        )
    patched.contract_hash = after_hash
    return patched


def _find_step(artifact: CapabilityArtifact, step_id: str) -> Step | None:
    return next((s for s in artifact.steps if s.id == step_id), None)


def _patch_target(step: Step, patch: dict[str, Any]) -> None:
    if step.target is None:
        raise ValueError(f"step {step.id!r} has no target to override")
    if "primary" in patch:
        step.target.primary = Locator(**patch["primary"])
    if "fallbacks" in patch:
        step.target.fallbacks = [Locator(**f) for f in patch["fallbacks"]]
    if "rationale" in patch:
        step.target.rationale = patch["rationale"]
