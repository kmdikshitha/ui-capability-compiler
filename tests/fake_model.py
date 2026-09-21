"""A scripted stand-in for Claude, for developing and testing everything
downstream of discovery without spending an API call on every iteration.

It drives the real loop: the real system prompt is rendered, the real tools are
called, the real guardrails run, and the real transcript is written. What it
does not do is decide anything -- it follows a fixed plan and resolves refs by
parsing the same screen rendering the model would have read.

The submitted discovery run is a real model run. This exists so that the
compiler, the executor and the tests are not blocked on one.
"""

from __future__ import annotations

import itertools
import re
from types import SimpleNamespace
from typing import Any

LINE = re.compile(r'^\[(?P<ref>e\d+)\] (?P<role>\w+)(?: "(?P<name>[^"]*)")?(?P<rest>.*)$')

_ids = itertools.count(1)


def _block(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(**fields)


def _parse(rendering: str) -> list[dict[str, str]]:
    found = []
    for line in rendering.splitlines():
        match = LINE.match(line.strip())
        if match:
            found.append({"ref": match.group("ref"), "role": match.group("role"),
                          "name": match.group("name") or "", "rest": match.group("rest") or ""})
    return found


class ScriptedClient:
    """Mimics client.messages.create for the subset the loop uses."""

    def __init__(self, plan: list[dict[str, Any]], outputs: dict[str, str] | None = None):
        self.plan = plan
        self.outputs = outputs or {}
        self.step = 0
        self.messages = SimpleNamespace(create=self._create)

    def _last_rendering(self, messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    for piece in block.get("content", []):
                        if isinstance(piece, dict) and piece.get("type") == "text":
                            return piece["text"]
        return ""

    def _resolve(self, rendering: str, want: dict[str, Any]) -> str | None:
        for element in _parse(rendering):
            if element["role"] != want["role"]:
                continue
            if want.get("name") and element["name"] != want["name"]:
                continue
            return element["ref"]
        return None

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        messages = kwargs["messages"]
        rendering = self._last_rendering(messages)

        if self.step >= len(self.plan):
            outputs = {name: self._read_cell(rendering, label) or value
                       for name, (label, value) in self.outputs.items()}
            return self._respond("finish", {"outputs": outputs,
                                            "summary": "scripted run complete"})

        entry = self.plan[self.step]
        self.step += 1
        tool = entry["tool"]
        args: dict[str, Any] = dict(entry.get("args") or {})

        if "target" in entry:
            ref = self._resolve(rendering, entry["target"])
            if ref is None:
                return self._respond(
                    "escalate",
                    {"reason": f"scripted target not on screen: {entry['target']}"})
            args["ref"] = ref
        return self._respond(tool, args)

    @staticmethod
    def _read_cell(rendering: str, row_label: str) -> str | None:
        """Read a confirmation value off the screen, the way the model is told to."""
        for element in _parse(rendering):
            if element["role"] == "cell" and f'row="{row_label}"' in element["rest"] \
                    and 'col="Value"' in element["rest"]:
                if element["name"] and element["name"] != row_label:
                    return element["name"]
        return None

    def _respond(self, tool: str, args: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            stop_reason="tool_use",
            stop_details=None,
            content=[
                _block(type="thinking", thinking=f"scripted: next action is {tool}"),
                _block(type="tool_use", id=f"call_{next(_ids)}", name=tool, input=args),
            ],
            usage=SimpleNamespace(cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


def open_subaccount_plan(inputs: dict[str, str]) -> ScriptedClient:
    """The flow a model discovers on the heritage tenant."""
    plan = [
        {"tool": "observe"},
        {"tool": "type", "target": {"role": "textbox", "name": "Operator ID"},
         "args": {"text": "OP-4471"}},
        {"tool": "click", "target": {"role": "button", "name": "Sign In"}},
        {"tool": "type", "target": {"role": "textbox", "name": "Member ID"},
         "args": {"text": inputs["member_id"]}},
        {"tool": "click", "target": {"role": "button", "name": "Search"}},
        {"tool": "click", "target": {"role": "link", "name": "Open Sub-Account"}},
        {"tool": "select", "target": {"role": "combobox", "name": "Account Type"},
         "args": {"value": inputs["account_type"]}},
        {"tool": "type", "target": {"role": "textbox", "name": "Initial Deposit"},
         "args": {"text": inputs["initial_deposit"]}},
        {"tool": "type", "target": {"role": "textbox", "name": "Nickname"},
         "args": {"text": inputs.get("nickname", "")}},
        {"tool": "click", "target": {"role": "button", "name": "Create Sub-Account"}},
    ]
    outputs = {"confirmation_number": ("Confirmation Number", ""),
               "new_account_number": ("New Account Number", "")}
    return ScriptedClient(plan, outputs)
