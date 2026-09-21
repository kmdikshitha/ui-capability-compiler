"""Checkpoint assertion.

A checkpoint is how replay knows it actually reached the state it expected
rather than assuming the click worked. Every wait here is a wait on a
condition with the artifact's own timeout -- there is no sleep() in the replay
path, because a sleep is a guess about how slow the app is today.
"""

from __future__ import annotations

import re
from typing import Any

from src.artifact.models import Checkpoint
from src.guardrails.redaction import redact_text
from src.surface.playwright_driver import PlaywrightSurface

SCAN_TIMEOUT_MS = 400


def describe(checkpoint: Checkpoint) -> str:
    if checkpoint.type == "element_visible":
        return f'{checkpoint.role} "{checkpoint.name}" visible'
    if checkpoint.type == "text_present":
        return f'text "{checkpoint.text}" present'
    return f"url matching {checkpoint.pattern}"


def assert_checkpoint(surface: PlaywrightSurface, checkpoint: Checkpoint) -> bool:
    """True if the condition holds within its timeout."""
    if checkpoint.type == "url_matches":
        return _wait_url(surface, checkpoint)
    return _wait_element(surface, checkpoint)


def observed_state(surface: PlaywrightSurface) -> str:
    """What was actually on screen, for a failure report.

    Redacted at construction rather than at the writer. This string is
    diagnostic text scraped off a live banking screen, and it is destined for
    result.json, the log and the operator's terminal alike -- redacting it once,
    here, means none of those three has to remember. Declared outputs are not
    touched: those are the contract, and the caller is entitled to them.
    """
    try:
        obs = surface.observe()
    except Exception as exc:  # pragma: no cover - the page is already broken
        return f"could not observe: {exc}"
    heading = next((e.name for e in obs.elements if e.role == "heading" and e.name), None)
    return redact_text(
        f"url={obs.url} heading={heading!r} text={obs.text_digest[:180]!r}")


def _locator_for(frame: Any, checkpoint: Checkpoint) -> Any:
    if checkpoint.type == "element_visible":
        return frame.get_by_role(checkpoint.role, name=checkpoint.name, exact=True)
    return frame.get_by_text(checkpoint.text, exact=False)


def _wait_element(surface: PlaywrightSurface, checkpoint: Checkpoint) -> bool:
    """Scan the frames cheaply, then wait properly on the one that has it.

    Waiting the full timeout on the wrong frame would burn the whole budget
    before looking at the frame the content actually landed in, so the scan
    comes first and the wait is spent where it can succeed.
    """
    frames = [frame for _, frame in surface._live_frames()]
    for frame in frames:
        try:
            if _locator_for(frame, checkpoint).count() > 0:
                return _wait(frame, checkpoint, checkpoint.timeout_ms)
        except Exception:
            continue
    # Not present anywhere yet. The work area is the deepest frame and is where
    # content arrives, so spend the remaining budget there.
    target = frames[-1] if frames else None
    return _wait(target, checkpoint, checkpoint.timeout_ms) if target else False


def _wait(frame: Any, checkpoint: Checkpoint, timeout_ms: int) -> bool:
    try:
        _locator_for(frame, checkpoint).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _wait_url(surface: PlaywrightSurface, checkpoint: Checkpoint) -> bool:
    pattern = re.compile(checkpoint.pattern or "")
    try:
        surface.page.wait_for_url(
            lambda url: bool(pattern.search(_path(url))), timeout=checkpoint.timeout_ms)
        return True
    except Exception:
        return bool(pattern.search(_path(surface.page.url)))


def _path(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).path or "/"


def matches_now(surface: PlaywrightSurface, checkpoint: Checkpoint) -> bool:
    """Non-waiting check, for classifying an anomaly against declared outcomes."""
    if checkpoint.type == "url_matches":
        return bool(re.search(checkpoint.pattern or "", _path(surface.page.url)))
    for _, frame in surface._live_frames():
        try:
            if _locator_for(frame, checkpoint).count() > 0:
                return True
        except Exception:
            continue
    return False
