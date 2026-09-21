"""Tenant overrides may patch mechanics, never the contract."""

from __future__ import annotations

import pytest

from src.artifact.models import ContractViolation
from src.artifact.overrides import apply_override, load_override


def test_locator_override_is_allowed(artifact) -> None:
    merged = apply_override(artifact, {
        "tenant_variant": "community",
        "steps": {"s6": {"target": {"primary": {
            "strategy": "role_name", "role": "link", "name": "New Sub-Account",
            "frame_path": ["shell", "workarea"]}}}},
    })
    assert merged.steps[5].target.primary.name == "New Sub-Account"
    assert merged.contract_hash == artifact.contract_hash
    assert merged.tenant_variant == "community"


def test_override_may_insert_a_step(artifact) -> None:
    merged = apply_override(artifact, {"insert_steps": [{"after": "s10", "step": {
        "id": "s10a", "action": "click", "risk": "irreversible",
        "target": {"primary": {"strategy": "role_name", "role": "button",
                               "name": "Acknowledge"}, "rationale": "tenant gate"}}}]})
    assert [s.id for s in merged.steps][-1] == "s10a"
    assert merged.contract_hash == artifact.contract_hash


def test_override_touching_outputs_is_refused(artifact) -> None:
    """The rule that stops one tenant silently changing what a capability returns.

    An extract locator is Locator-shaped and reads like a selector tweak, which
    is exactly why it needs a mechanical guard rather than a reviewer's
    attention: this patch would make the capability return a different column
    while every calling agent carried on believing the contract.
    """
    with pytest.raises(ContractViolation) as caught:
        apply_override(artifact, {"outputs": {artifact.outputs[0].name: {"extract": {
            "strategy": "table_anchor", "role": "cell", "header": "Value",
            "row_key": "Opening Balance", "frame_path": ["shell", "workarea"]}}}})
    assert "mechanics only" in str(caught.value)


def test_override_leaving_the_contract_alone_is_accepted(artifact) -> None:
    """The same machinery must not reject a legitimate mechanics-only patch."""
    merged = apply_override(artifact, {"steps": {"s8": {"value": "0.00"}}})
    assert merged.steps[7].value == "0.00"
    assert merged.contract_hash == artifact.contract_hash


def test_shipped_community_override_preserves_the_contract(artifact) -> None:
    override = load_override("community")
    assert override is not None, "tenant-overrides/community.yaml is missing"
    merged = apply_override(artifact, override)
    assert merged.contract_hash == artifact.contract_hash
    assert len(merged.steps) > len(artifact.steps)


def test_override_on_unknown_step_is_rejected(artifact) -> None:
    with pytest.raises(ValueError):
        apply_override(artifact, {"steps": {"s999": {"value": "x"}}})
