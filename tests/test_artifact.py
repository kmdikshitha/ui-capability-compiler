"""Artifact schema, canonical serialization, versioning and the contract hash."""

from __future__ import annotations

import json

import pytest

from src.artifact import store
from src.artifact.models import CapabilityArtifact, canonical_json


def test_round_trip_is_byte_identical(artifact: CapabilityArtifact, tmp_path) -> None:
    path = store.save(artifact.model_copy(deep=True), tmp_path)
    reloaded = store.load(path)
    assert canonical_json(reloaded.model_dump(mode="json")) == path.read_text()
    assert reloaded.model_dump() == artifact.model_dump()


def test_contract_hash_survives_reserialization(artifact: CapabilityArtifact, tmp_path) -> None:
    before = artifact.compute_contract_hash()
    reloaded = store.load(store.save(artifact.model_copy(deep=True), tmp_path))
    assert reloaded.compute_contract_hash() == before
    assert reloaded.verify_contract_hash()


def test_contract_hash_ignores_mechanics(artifact: CapabilityArtifact) -> None:
    """Changing how a control is found must not change what the capability is."""
    before = artifact.compute_contract_hash()
    patched = artifact.model_copy(deep=True)
    patched.steps[5].target.primary.name = "Something Else Entirely"
    patched.steps.append(patched.steps[-1].model_copy(deep=True))
    assert patched.compute_contract_hash() == before


def test_contract_hash_tracks_the_contract(artifact: CapabilityArtifact) -> None:
    before = artifact.compute_contract_hash()
    patched = artifact.model_copy(deep=True)
    patched.outputs[0].name = "renamed_output"
    assert patched.compute_contract_hash() != before


def test_canonical_json_is_sorted_and_compact() -> None:
    text = canonical_json({"b": 1, "a": {"d": 2, "c": 3}})
    assert text == '{"a":{"c":3,"d":2},"b":1}'


def test_artifacts_are_immutable(artifact: CapabilityArtifact, tmp_path) -> None:
    store.save(artifact.model_copy(deep=True), tmp_path)
    with pytest.raises(FileExistsError):
        store.save(artifact.model_copy(deep=True), tmp_path)


def test_next_version_increments(artifact: CapabilityArtifact, tmp_path) -> None:
    assert store.next_version(artifact.capability_id, tmp_path) == 1
    store.save(artifact.model_copy(deep=True), tmp_path)
    assert store.next_version(artifact.capability_id, tmp_path) == 2
    second = artifact.model_copy(deep=True)
    second.version = 2
    store.save(second, tmp_path)
    assert store.versions(artifact.capability_id, tmp_path) == [1, 2]


def test_status_change_does_not_need_a_new_version(artifact, tmp_path) -> None:
    """Promoting a draft is a review decision about an existing recording."""
    draft = artifact.model_copy(deep=True)
    draft.status = "draft"
    path = store.save(draft, tmp_path)
    draft.status = "approved"
    store.save_status_change(draft, tmp_path)
    assert store.load(path).status == "approved"
    assert store.versions(artifact.capability_id, tmp_path) == [1]


def test_every_target_carries_a_rationale(artifact: CapabilityArtifact) -> None:
    """The rationale is graded; an empty one is a silent regression."""
    for step in artifact.steps:
        if step.target is not None:
            assert step.target.rationale.strip(), f"{step.id} has no targeting rationale"
