"""The discovery loop: observe -> decide -> act.

This is the only place a model decides anything. Everything downstream of a
successful run -- the compiler, the executor -- is deterministic, and the import
boundary test in tests/ mechanically proves replay never reaches back in here.

Every tool call passes through the same guardrails function replay uses. That
shared path is deliberate: discovery is the more dangerous of the two, because
the model is exploring a live application rather than following a reviewed
script.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

import anthropic

from src.agent.prompts import SYSTEM, goal_prompt
from src.agent.tools import ACTING_TOOLS, TERMINAL_TOOLS, TOOLS, render
from src.guardrails.allowlist import Policy, check
from src.guardrails.risk import classify
from src.surface.playwright_driver import PlaywrightSurface
from src.types import INTERACTIVE_ROLES, Action, Observation

log = logging.getLogger("discovery")

DEFAULT_MODEL = "claude-sonnet-5"
# Every response is a single tool call, so this only needs headroom for one
# call plus whatever thinking the model does. It is a ceiling, not a target.
MAX_TOKENS = 4000
MAX_STEPS = 25
WALL_CLOCK_S = 120
DEAD_END_REPEATS = 3

Status = Literal["finished", "escalated", "max_steps", "timeout", "dead_end", "error"]


@dataclass
class DiscoveryResult:
    status: Status
    reason: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    turns: int = 0
    final_observation: Observation | None = None


def _cached_tools() -> list[dict[str, Any]]:
    """Tools are a frozen, ordered list; the breakpoint on the last one caches
    the whole tool block across the twenty-odd turns of a run."""
    tools = [dict(t) for t in TOOLS]
    tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def run_discovery(
    surface: PlaywrightSurface,
    *,
    goal: str,
    inputs: dict[str, str],
    entry_url: str,
    output_names: list[str],
    policy: Policy,
    record: Callable[[dict[str, Any]], None],
    screenshot_path: Callable[[int], str],
    model: str = DEFAULT_MODEL,
    max_steps: int = MAX_STEPS,
    wall_clock_s: int = WALL_CLOCK_S,
    max_tokens: int = MAX_TOKENS,
    client: Any | None = None,
) -> DiscoveryResult:
    client = client or anthropic.Anthropic()
    started = time.monotonic()

    messages: list[dict[str, Any]] = [
        {"role": "user",
         "content": goal_prompt(goal, inputs, entry_url, output_names)}
    ]

    current: Observation | None = None
    digests: list[str] = []
    turn = 0
    last_failed = False

    # The caller supplies the entry point; the agent explores from there. Doing
    # this here rather than asking the model to navigate keeps the first step of
    # every recording identical and gives the compiler its entry_url_pattern.
    entry = Action(type="navigate", url=entry_url)
    decision = check(entry, current_url="", policy=policy, risk="safe", attended=True)
    if not decision.allowed:
        return DiscoveryResult("error", f"entry point blocked: {decision.reason}")
    entry_result = surface.act(entry)
    if not entry_result.ok:
        return DiscoveryResult("error", f"could not reach entry point: {entry_result.error}")
    current = surface.observe()
    record({"turn": 0, "tool": "navigate", "reasoning": "entry point supplied by the caller",
            "args": {"url": entry_url}, "element": None,
            "result": entry_result.model_dump(), "guardrail": decision.model_dump(),
            "observation": current.model_dump(), "screenshot_path": None})

    while True:
        if turn >= max_steps:
            return DiscoveryResult("max_steps", f"stopped after {turn} steps", turns=turn,
                                   final_observation=current)
        if time.monotonic() - started > wall_clock_s:
            return DiscoveryResult("timeout", f"wall clock exceeded {wall_clock_s}s",
                                   turns=turn, final_observation=current)

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            tools=_cached_tools(),
            thinking={"type": "adaptive", "display": "summarized"},
            messages=messages,
        )

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            return DiscoveryResult("error", f"model refused: {getattr(detail, 'category', None)}",
                                   turns=turn, final_observation=current)
        if response.stop_reason == "max_tokens":
            return DiscoveryResult("error", "model response hit max_tokens", turns=turn,
                                   final_observation=current)

        # Echo the assistant turn back verbatim, thinking blocks included.
        messages.append({"role": "assistant", "content": response.content})

        reasoning = " ".join(
            (block.thinking if block.type == "thinking" else block.text).strip()
            for block in response.content
            if block.type in ("thinking", "text")
        ).strip()

        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            return DiscoveryResult("dead_end", "model stopped without calling a tool",
                                   turns=turn, final_observation=current)

        results: list[dict[str, Any]] = []
        for call in calls:
            turn += 1
            tool, args = call.name, dict(call.input or {})

            if tool in TERMINAL_TOOLS:
                record({"turn": turn, "tool": tool, "reasoning": reasoning, "args": args,
                        "observation": current.model_dump() if current else None})
                if tool == "finish":
                    return DiscoveryResult("finished", args.get("summary", ""),
                                           outputs={k: str(v) for k, v in
                                                    (args.get("outputs") or {}).items()},
                                           turns=turn, final_observation=current)
                return DiscoveryResult("escalated", args.get("reason", "model escalated"),
                                       turns=turn, final_observation=current)

            outcome = _run_tool(surface, tool, args, current, policy, inputs)
            current = outcome.observation or current

            shot = None
            if outcome.failed:
                shot = surface.screenshot(screenshot_path(turn))

            record({
                "turn": turn, "tool": tool, "reasoning": reasoning, "args": args,
                "element": outcome.element, "result": outcome.result,
                "guardrail": outcome.guardrail,
                "observation": outcome.before.model_dump() if outcome.before else None,
                "screenshot_path": shot,
            })

            content: list[dict[str, Any]] = [{"type": "text", "text": outcome.text}]
            # A screenshot is attached only on the turn after a failed action:
            # cheap in the common case, and the failure image is evidence we
            # need on disk regardless.
            if last_failed and shot and Path(shot).exists():
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png",
                               "data": base64.standard_b64encode(
                                   Path(shot).read_bytes()).decode()},
                })
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": content, "is_error": outcome.failed})
            last_failed = outcome.failed

            if tool in ACTING_TOOLS and current is not None:
                digests.append(_state_signature(current))
                if len(digests) >= DEAD_END_REPEATS and len(set(digests[-DEAD_END_REPEATS:])) == 1:
                    return DiscoveryResult(
                        "dead_end",
                        f"screen unchanged across {DEAD_END_REPEATS} actions",
                        turns=turn, final_observation=current)

        messages.append({"role": "user", "content": results})


def _state_signature(obs: Observation) -> str:
    """What "the screen is unchanged" actually means.

    Visible text alone is not enough: filling in three form fields changes
    nothing a text digest can see, and treating that as a dead end would stop
    the run in the middle of every form on the surface. The values of the
    controls are part of the state.
    """
    values = ";".join(
        f"{e.role}:{e.name}={e.value or ''}"
        for e in obs.elements if e.role in INTERACTIVE_ROLES
    )
    return f"{obs.url}||{obs.text_digest}||{values}"


@dataclass
class _ToolOutcome:
    text: str
    failed: bool = False
    observation: Observation | None = None
    before: Observation | None = None
    element: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    guardrail: dict[str, Any] | None = None


def _run_tool(surface: PlaywrightSurface, tool: str, args: dict[str, Any],
              current: Observation | None, policy: Policy,
              inputs: dict[str, str]) -> _ToolOutcome:
    if tool == "observe":
        obs = surface.observe()
        return _ToolOutcome(render(obs), observation=obs, before=current)

    ref = args.get("ref")
    element = None
    if ref:
        try:
            element = surface.element_for_ref(ref)
        except LookupError as exc:
            return _ToolOutcome(f"{exc}. Call observe to get current refs.", failed=True,
                                before=current)

    action = Action(
        type="type" if tool == "type" else tool,  # tool names already match ActionType
        ref=ref,
        value=args.get("text") or args.get("value"),
        url=args.get("url"),
    )
    risk = classify(action.type, element.name if element else None)

    # Same gate replay uses. Discovery is attended and explicitly a sandbox.
    decision = check(action, current_url=(current.url if current else ""), policy=policy,
                     risk=risk, artifact_status="draft", attended=True)
    guardrail = decision.model_dump()
    if not decision.allowed:
        return _ToolOutcome(
            f"Blocked by policy: {decision.reason}. Choose a different action.",
            failed=True, before=current, element=element.model_dump() if element else None,
            guardrail=guardrail)

    result = surface.act(action)
    if not result.ok:
        return _ToolOutcome(f"Action failed: {result.error}", failed=True, before=current,
                            element=element.model_dump() if element else None,
                            result=result.model_dump(), guardrail=guardrail)

    if tool == "read":
        return _ToolOutcome(f"value: {result.read_value!r}", observation=current,
                            before=current,
                            element=element.model_dump() if element else None,
                            result=result.model_dump(), guardrail=guardrail)

    # Acting tools return the new screen, which saves the model a turn and gives
    # the compiler a reliable "observation that followed this step".
    obs = surface.observe()
    return _ToolOutcome(render(obs), observation=obs, before=current,
                        element=element.model_dump() if element else None,
                        result=result.model_dump(), guardrail=guardrail)
