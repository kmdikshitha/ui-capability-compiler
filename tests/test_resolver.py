"""The locator ladder against the real hostile surface."""

from __future__ import annotations

import pytest

from src.artifact.models import Locator, TargetSpec
from src.replay.resolver import resolve
from src.types import Action

WORKAREA = ["shell", "workarea"]


@pytest.fixture
def on_form(surface, clean_faults, mock_app):
    """Sign in and land on the sub-account form, where the unnamed controls are."""
    surface.act(Action(type="navigate", url=f"{mock_app}/login"))
    obs = surface.observe()
    surface.act(Action(type="click",
                       ref=next(e.ref for e in obs.elements if e.role == "button")))
    surface.act(Action(type="navigate", url=f"{mock_app}/members/12345/subaccount/new"))
    surface.observe()
    return surface


def test_rung_one_resolves_a_named_control(on_form) -> None:
    spec = TargetSpec(primary=Locator(strategy="role_name", role="button",
                                      name="Create Sub-Account", frame_path=WORKAREA),
                      rationale="x")
    found = resolve(on_form, spec)
    assert found.ok and found.rung == "role_name"


def test_falls_through_to_the_label_rung(on_form) -> None:
    """A control with no accessible name is still reachable by its row label."""
    spec = TargetSpec(
        primary=Locator(strategy="role_name", role="textbox",
                        name="Initial Deposit", frame_path=WORKAREA),
        fallbacks=[Locator(strategy="label_adjacent", role="textbox",
                           label="Initial Deposit", frame_path=WORKAREA)],
        rationale="x")
    found = resolve(on_form, spec)
    assert found.ok
    assert found.rung == "label_adjacent"
    assert found.attempted == ["role_name", "label_adjacent"]


def test_exhausting_the_ladder_never_guesses(on_form) -> None:
    spec = TargetSpec(
        primary=Locator(strategy="role_name", role="button",
                        name="No Such Control", frame_path=WORKAREA),
        fallbacks=[Locator(strategy="label_adjacent", role="textbox",
                           label="No Such Label", frame_path=WORKAREA)],
        rationale="x")
    found = resolve(on_form, spec)
    assert not found.ok
    assert found.locator is None and found.bbox is None
    assert found.attempted == ["role_name", "label_adjacent"]


def test_bbox_is_not_walked_by_default(on_form) -> None:
    """Coordinates always 'resolve'; walking them by default would mean a
    vanished control still gets clicked wherever it used to be."""
    spec = TargetSpec(
        primary=Locator(strategy="role_name", role="button",
                        name="No Such Control", frame_path=WORKAREA),
        fallbacks=[Locator(strategy="bbox", role="button", bbox=(10, 10, 20, 20),
                           frame_path=WORKAREA)],
        rationale="x")
    assert not resolve(on_form, spec).ok
    assert resolve(on_form, spec, allow_bbox=True).ok


def test_table_anchor_reads_a_cell_by_row_and_column(surface, clean_faults, mock_app) -> None:
    surface.act(Action(type="navigate", url=f"{mock_app}/login"))
    obs = surface.observe()
    surface.act(Action(type="click",
                       ref=next(e.ref for e in obs.elements if e.role == "button")))
    surface.act(Action(type="navigate", url=f"{mock_app}/members/12345"))
    surface.observe()
    spec = TargetSpec(primary=Locator(strategy="table_anchor", role="cell",
                                      header="Available Balance", row_key="Savings",
                                      frame_path=WORKAREA), rationale="x")
    found = resolve(surface, spec)
    assert found.ok and found.rung == "table_anchor"
    assert surface.act_on_locator(found.locator, "read", None).read_value == "8,915.20"
