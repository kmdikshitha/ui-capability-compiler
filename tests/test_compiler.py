"""Compiler rules: parameterization, route canonicalization, checkpoint choice."""

from __future__ import annotations

from src.artifact.compiler import (
    build_target, canonical_path, parameterize, path_pattern, stable_heading,
)
from src.types import Element, Observation

INPUTS = {"member_id": "12345", "initial_deposit": "250.00"}


def test_exact_values_become_templates() -> None:
    assert parameterize("12345", INPUTS) == "{{ inputs.member_id }}"
    assert parameterize("250.00", INPUTS) == "{{ inputs.initial_deposit }}"


def test_incidental_values_stay_literal() -> None:
    """Only exact matches are parameterized; an operator id is not an input."""
    assert parameterize("OP-4471", INPUTS) == "OP-4471"
    assert parameterize("Member 12345 details", INPUTS) == "Member 12345 details"


def test_routes_are_canonicalized() -> None:
    url = "http://localhost:8000/members/12345/subaccount/new"
    assert canonical_path(url, INPUTS) == "/members/:member_id/subaccount/new"
    assert path_pattern(url, INPUTS) == r"^/members/[^/]+/subaccount/new$"


def test_ladder_is_built_best_rung_first() -> None:
    named = Element(ref="e1", role="button", name="Create Sub-Account",
                    name_source="aria", nth=0, frame_path=["shell", "workarea"])
    spec = build_target(named, (10, 20, 30, 40))
    assert spec.primary.strategy == "role_name"
    assert [f.strategy for f in spec.fallbacks] == ["ordinal", "bbox"]
    assert spec.rationale


def test_unnamed_control_targets_by_its_row_label() -> None:
    """The case the hostile mock app exists to produce."""
    unnamed = Element(ref="e2", role="textbox", name="Initial Deposit",
                      name_source="adjacent", nth=1, frame_path=["shell", "workarea"])
    spec = build_target(unnamed, None)
    assert spec.primary.strategy == "label_adjacent"
    assert spec.primary.label == "Initial Deposit"


def test_cell_targets_by_row_and_column() -> None:
    cell = Element(ref="e3", role="cell", name="8,915.20", row_key="Savings",
                   column="Available Balance", column_index=2, nth=5)
    spec = build_target(cell, None)
    assert spec.primary.strategy == "table_anchor"
    assert (spec.primary.row_key, spec.primary.header) == ("Savings", "Available Balance")


def test_checkpoint_heading_never_embeds_an_input() -> None:
    """A heading carrying the member id would pass only for the recorded member."""
    obs = Observation(url="u", title="t", text_digest="", elements=[
        Element(ref="e1", role="heading", name="Member 12345 — A. Rivera"),
        Element(ref="e2", role="heading", name="Sub-Account Confirmation"),
    ])
    assert stable_heading(obs, INPUTS) == "Sub-Account Confirmation"


def test_credentials_are_never_inlined_into_an_artifact() -> None:
    """Found by the first real discovery run, which filled the login password box.

    A capability references a credential; it never carries one. Without this the
    compiler writes whatever the model typed straight into a committed file.
    """
    from src.artifact.compiler import CREDENTIAL_FIELDS, secret_ref

    for name in ("Password", "PIN", "Security Code", "API Key", "SSN"):
        assert CREDENTIAL_FIELDS.search(name), f"{name!r} should be treated as a credential"
    for name in ("Member ID", "Initial Deposit", "Nickname", "Account Type"):
        assert not CREDENTIAL_FIELDS.search(name), f"{name!r} is not a credential"

    assert secret_ref("Password") == "{{ secrets.password }}"
    assert secret_ref("Security Code") == "{{ secrets.security_code }}"


def test_secrets_resolve_from_the_environment_and_are_redacted(monkeypatch) -> None:
    from src.guardrails.redaction import clear_sensitive, redact_text
    from src.replay.executor import render_template

    clear_sensitive()
    monkeypatch.setenv("CAPABILITY_SECRET_PASSWORD", "s3cret-value")
    assert render_template("{{ secrets.password }}", {}) == "s3cret-value"
    # Resolving it registers it, so nothing downstream can write it to disk.
    assert "s3cret-value" not in redact_text("operator typed s3cret-value")


def test_an_unset_secret_resolves_empty_rather_than_leaking_the_template() -> None:
    from src.replay.executor import render_template
    assert render_template("{{ secrets.nothing_set }}", {}) == ""
