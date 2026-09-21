"""Tool definitions handed to Claude, and the compact observation rendering.

Only low-level primitives. There is deliberately no search_member() or
open_subaccount() tool: a semantic tool would mean the flow was discovered by
whoever wrote the tool list, not by the model, and the recorded artifact would
be a description of our own assumptions.

`select` is here although it is not a semantic tool: a native <select> cannot be
driven by clicking, and the action taxonomy and allowlist both already carry it.
"""

from __future__ import annotations

from typing import Any

from src.types import INTERACTIVE_ROLES, Observation

MAX_RENDERED_ELEMENTS = 70

TOOLS: list[dict[str, Any]] = [
    {
        "name": "observe",
        "description": (
            "Re-read the current screen and return the elements you can act on. "
            "Acting tools already return the new screen, so use this only when you "
            "want to look again without doing anything."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "click",
        "description": "Click an element. Returns the screen as it is after the click.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "Element ref, e.g. e12."}},
            "required": ["ref"],
        },
    },
    {
        "name": "type",
        "description": "Type text into a textbox, replacing whatever is in it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref of the textbox."},
                "text": {"type": "string", "description": "Text to enter."},
            },
            "required": ["ref", "text"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown by its visible label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref of the combobox."},
                "value": {"type": "string", "description": "The option label to select."},
            },
            "required": ["ref", "value"],
        },
    },
    {
        "name": "navigate",
        "description": "Go to a URL directly. Prefer clicking the UI where a control exists.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "read",
        "description": "Read the text or value of one element without changing anything.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Call this once the goal is complete and you are looking at the screen "
            "that proves it. Report every declared output, reading each value off "
            "the screen you are on rather than from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "outputs": {
                    "type": "object",
                    "description": "Declared output name -> the value shown on screen.",
                    "additionalProperties": {"type": "string"},
                },
                "summary": {"type": "string", "description": "One line on what you did."},
            },
            "required": ["outputs"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Call this when you cannot safely proceed: the screen is not what you "
            "expected, a step needs a decision you should not make, or you are stuck."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

ACTING_TOOLS = {"click", "type", "select", "navigate"}
TERMINAL_TOOLS = {"finish", "escalate"}


def render(obs: Observation) -> str:
    """The compact screen rendering the model sees.

    Filtered and indexed, not raw HTML and not the whole accessibility tree. A
    control whose label came from its table row is marked, so the model knows
    the label is positional rather than a real accessible name.
    """
    lines = [f"url: {obs.url}", f"title: {obs.title}"]
    shown = 0
    for element in obs.elements:
        if shown >= MAX_RENDERED_ELEMENTS:
            lines.append("... (more elements omitted)")
            break
        role, name = element.role, element.name.strip()
        keep = (
            role in INTERACTIVE_ROLES
            or role in ("heading", "alert", "dialog")
            or (role == "cell" and name)
        )
        if not keep:
            continue
        piece = f"[{element.ref}] {role}"
        if name:
            piece += f' "{name}"'
        if element.name_source == "adjacent":
            piece += " (label from row)"
        if element.value:
            piece += f' = "{element.value}"'
        if role == "cell" and element.row_key and element.column:
            piece += f' row="{element.row_key}" col="{element.column}"'
        if not element.enabled:
            piece += " [disabled]"
        lines.append(piece)
        shown += 1
    lines.append(f"text: {obs.text_digest[:700]}")
    return "\n".join(lines)
