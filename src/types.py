"""Shared primitives.

`Surface` is the seam between how we perceive and act on a surface and the
recorded flow. `playwright_driver` is the only implementation and the only
module in the project that imports playwright. A desktop driver would implement
the same three methods against the OS accessibility API, and no artifact would
need to change.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel

Role = Literal[
    "button", "textbox", "link", "heading", "cell", "row", "rowgroup", "table",
    "checkbox", "radio", "combobox", "option", "dialog", "alert", "region",
    "list", "listitem", "img", "form", "group", "banner", "navigation", "main",
    "separator", "paragraph", "text", "generic",
]

KNOWN_ROLES: frozenset[str] = frozenset(Role.__args__)

# Roles a human operator actually drives. Used to decide which elements deserve
# an adjacency-derived label and which are structural noise.
INTERACTIVE_ROLES: frozenset[str] = frozenset(
    {"button", "textbox", "link", "checkbox", "radio", "combobox", "option"}
)

# Where an element's name came from. Legacy surfaces routinely ship controls
# with no accessible name at all; the compiler picks a locator strategy off
# this field, so it has to travel with the observation.
NameSource = Literal["aria", "adjacent", "none"]


class Element(BaseModel):
    ref: str                      # opaque handle, valid only within one Observation
    role: Role
    name: str = ""                # accessible name, or an adjacency-derived label
    name_source: NameSource = "aria"
    value: str | None = None
    row_key: str | None = None    # for cells: the row's leading cell text
    column: str | None = None     # for cells: the column header text
    column_index: int | None = None
    nth: int = 0                  # index among same-role elements in this frame
    frame_path: list[str] = []    # iframe chain, outermost first
    enabled: bool = True


class Observation(BaseModel):
    url: str
    title: str
    elements: list[Element]
    text_digest: str              # visible text, truncated, for outcome detection
    screenshot_path: str | None = None


ActionType = Literal["click", "type", "navigate", "read", "select"]


class Action(BaseModel):
    type: ActionType
    ref: str | None = None        # discovery acts by ref; replay resolves a Locator first
    value: str | None = None
    url: str | None = None


class ActResult(BaseModel):
    ok: bool
    error: str | None = None
    read_value: str | None = None
    # Captured at action time so the compiler can record a bbox rung without
    # paying for a bounding box on every element of every observation.
    bounds: tuple[int, int, int, int] | None = None


class Surface(Protocol):
    def observe(self) -> Observation: ...
    def act(self, action: Action) -> ActResult: ...
    def close(self) -> None: ...
