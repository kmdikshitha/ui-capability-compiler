"""Transcript -> CapabilityArtifact.

Deterministic. No second model pass: everything here is a rule over what the
discovery run actually observed and did. The model's job was to find the flow;
turning the flow into a contract is ours, because a compiler that asks a model
what it just did inherits the model's non-determinism at exactly the moment we
are trying to leave it behind.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from src.artifact.models import (
    BusinessOutcome, CapabilityArtifact, Checkpoint, InputParam, Locator,
    LocatorStrategy, OutputParam, RecoverableCondition, Step, TargetSpec,
)
from src.guardrails.risk import classify
from src.types import Element, Observation

ACTING_TOOLS = {"click", "type", "select", "navigate"}

# Controls whose value is a credential. Whatever the model typed into one of
# these is never written to the artifact: it is replaced by a secrets reference
# that replay resolves from the environment at run time.
#
# This is not hypothetical. The first real discovery run filled the login form's
# password box, and without this rule the compiler wrote that value straight
# into a committed artifact.
CREDENTIAL_FIELDS = re.compile(
    r"(?i)\b(password|passcode|pin|secret|token|api[\s_-]?key|ssn|security\s*code)\b")


def secret_ref(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "credential"
    return f"{{{{ secrets.{slug} }}}}"


class CompileError(Exception):
    pass


# -- transcript --------------------------------------------------------------

def load_transcript(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def load_spec(capability_id: str, directory: str | Path = "capability_specs") -> dict[str, Any]:
    return yaml.safe_load((Path(directory) / f"{capability_id}.yaml").read_text())


# -- parameterization --------------------------------------------------------

def parameterize(value: str | None, inputs: dict[str, str]) -> str | None:
    """Replace launch input values with templates.

    Exact match only. A value derived mid-flow -- an account number the app
    generated and a later step echoes back -- will not be caught here and stays
    a literal; see REPORT.md, Cuts.
    """
    if value is None:
        return None
    text = str(value)
    for name, raw in sorted(inputs.items(), key=lambda kv: -len(str(kv[1]))):
        raw = str(raw)
        if raw and text == raw:
            return f"{{{{ inputs.{name} }}}}"
    return text


def canonical_path(url: str, inputs: dict[str, str]) -> str:
    """/members/12345 -> /members/:member_id, using the launch inputs."""
    path = urlparse(url).path or "/"
    for name, raw in sorted(inputs.items(), key=lambda kv: -len(str(kv[1]))):
        raw = str(raw)
        if raw and raw in path:
            path = path.replace(raw, f":{name}")
    return path


def path_pattern(url: str, inputs: dict[str, str]) -> str:
    """A regex for the canonicalized path, so a checkpoint can assert on it."""
    canonical = canonical_path(url, inputs)
    parts = [re.escape(p) if not p.startswith(":") else r"[^/]+"
             for p in canonical.split("/")]
    return "^" + "/".join(parts) + "$"


# -- targeting ---------------------------------------------------------------

RATIONALES: dict[LocatorStrategy, str] = {
    "role_name": "Accessible role and name are stable across renders; the element "
                 "id regenerates on every request and is unusable.",
    "label_adjacent": "The control carries no accessible name. Its label is the leading "
                      "cell of its row, which survives id churn and re-rendering.",
    "table_anchor": "Addressed by row key and column header rather than position, so "
                    "the cell is still found when rows are added or reordered.",
    "ordinal": "No name or label is available; positional within the frame. Brittle if "
               "the form gains a control before this one.",
    "bbox": "Recorded coordinates. Never trusted alone -- using this rung means every "
            "structural rung failed and the capability needs re-recording.",
}


def build_target(element: Element, bounds: tuple[int, int, int, int] | None) -> TargetSpec:
    """Emit the full ladder for one element, best rung first."""
    frame = list(element.frame_path)
    rungs: dict[LocatorStrategy, Locator] = {}

    if element.name and element.name_source == "aria":
        rungs["role_name"] = Locator(strategy="role_name", role=element.role,
                                     name=element.name, frame_path=frame)
    if element.name and element.name_source == "adjacent":
        rungs["label_adjacent"] = Locator(strategy="label_adjacent", role=element.role,
                                          label=element.name, frame_path=frame)
    if element.role == "cell" and element.row_key and element.column:
        rungs["table_anchor"] = Locator(strategy="table_anchor", role="cell",
                                        header=element.column, row_key=element.row_key,
                                        frame_path=frame)
    rungs["ordinal"] = Locator(strategy="ordinal", role=element.role,
                               index=element.nth, frame_path=frame)
    if bounds:
        rungs["bbox"] = Locator(strategy="bbox", role=element.role,
                                bbox=tuple(bounds), frame_path=frame)

    # A cell's accessible name is its own content, so role_name targeting on a
    # cell means "find the cell that says 8,915.20" -- which only works while
    # the balance is unchanged. Address cells by where they sit instead.
    preference = ("table_anchor", "role_name", "label_adjacent", "ordinal", "bbox") \
        if element.role == "cell" else \
        ("role_name", "label_adjacent", "table_anchor", "ordinal", "bbox")
    ordered = [rungs[s] for s in preference if s in rungs]
    primary, fallbacks = ordered[0], ordered[1:]
    return TargetSpec(primary=primary, fallbacks=fallbacks,
                      rationale=RATIONALES[primary.strategy])


# -- checkpoints -------------------------------------------------------------

def stable_heading(obs: Observation, inputs: dict[str, str]) -> str | None:
    """The most prominent heading that does not embed an input value.

    "New Sub-Account" is a checkpoint. "Member 12345 - A. Rivera" is not: it
    would pass only for the member the capability was recorded against, which
    is the difference between a replay and a memory.
    """
    values = [str(v) for v in inputs.values() if str(v)]
    for element in obs.elements:
        if element.role != "heading" or not element.name.strip():
            continue
        if any(v in element.name for v in values):
            continue
        return element.name.strip()
    return None


def derive_checkpoint(next_obs: Observation, prev_url: str,
                      inputs: dict[str, str]) -> Checkpoint | None:
    heading = stable_heading(next_obs, inputs)
    if heading:
        return Checkpoint(type="element_visible", role="heading", name=heading)
    if urlparse(next_obs.url).path != urlparse(prev_url).path:
        return Checkpoint(type="url_matches", pattern=path_pattern(next_obs.url, inputs))
    return None


# -- compile -----------------------------------------------------------------

def compile_artifact(
    transcript: list[dict[str, Any]],
    spec: dict[str, Any],
    inputs: dict[str, str],
    *,
    version: int,
    model: str,
    run_id: str | None = None,
    tenant_variant: str = "base",
) -> CapabilityArtifact:
    acted = [r for r in transcript
             if r.get("tool") in ACTING_TOOLS and (r.get("result") or {}).get("ok")]
    if not acted:
        raise CompileError("transcript contains no successful actions")

    observations = [Observation(**r["observation"]) for r in transcript if r.get("observation")]
    if not observations:
        raise CompileError("transcript contains no observations")
    final_obs = observations[-1]

    # The observation that followed each action, for checkpoint derivation.
    following: dict[int, Observation] = {}
    for position, record in enumerate(transcript):
        if record is acted[0] or record.get("tool") in ACTING_TOOLS:
            nxt = next((Observation(**r["observation"])
                        for r in transcript[position + 1:] if r.get("observation")), final_obs)
            following[id(record)] = nxt

    steps: list[Step] = []
    baseline: dict[str, LocatorStrategy] = {}
    for index, record in enumerate(acted, start=1):
        tool = record["tool"]
        args = record.get("args") or {}
        element = Element(**record["element"]) if record.get("element") else None
        bounds = (record.get("result") or {}).get("bounds")
        step_id = f"s{index}"

        target = build_target(element, bounds) if element else None
        risk = classify(tool, element.name if element else None)
        prev_url = Observation(**record["observation"]).url if record.get("observation") else ""
        next_obs = following.get(id(record), final_obs)

        # A checkpoint after every irreversible step and after the last step.
        checkpoint = None
        if risk == "irreversible" or index == len(acted):
            checkpoint = derive_checkpoint(next_obs, prev_url, inputs)

        raw_value = args.get("text") or args.get("value")
        is_credential = element is not None and bool(
            CREDENTIAL_FIELDS.search(element.name or ""))
        value = secret_ref(element.name) if is_credential \
            else parameterize(raw_value, inputs)

        steps.append(Step(
            id=step_id,
            action=tool,
            target=target,
            value=value,
            url=(_template_url(args.get("url"), inputs) if tool == "navigate" else None),
            risk=risk,
            checkpoint=checkpoint,
            note=("credential; resolved from the environment at run time"
                  if is_credential else (element.name or None) if element else None),
        ))
        if target:
            baseline[step_id] = target.primary.strategy

    entry_url = next((Observation(**r["observation"]).url
                      for r in transcript if r.get("observation")), "")
    success = derive_checkpoint(final_obs, "", inputs)
    if success is None:
        raise CompileError("could not derive a success checkpoint from the final observation")

    declared_inputs = [InputParam(**i) for i in spec.get("inputs", [])]
    outputs = _derive_outputs(spec, transcript, final_obs)

    artifact = CapabilityArtifact(
        capability_id=spec["capability_id"],
        version=version,
        status="draft",
        description=" ".join(spec.get("description", "").split()),
        target_app=spec.get("target_app", "unknown"),
        tenant_variant=tenant_variant,
        entry_url_pattern=canonical_path(entry_url, inputs),
        preconditions=[],
        inputs=declared_inputs,
        outputs=outputs,
        steps=steps,
        success=success,
        business_outcomes=[BusinessOutcome(**b) for b in spec.get("business_outcomes", [])],
        recoverable=[RecoverableCondition(**r) for r in spec.get("recoverable", [])],
        baseline_rungs=baseline,
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        recorded_by_model=model,
        discovery_run_id=run_id,
    )
    return artifact.with_contract_hash()


def _template_url(url: str | None, inputs: dict[str, str]) -> str | None:
    if not url:
        return None
    for name, raw in sorted(inputs.items(), key=lambda kv: -len(str(kv[1]))):
        raw = str(raw)
        if raw and raw in url:
            url = url.replace(raw, f"{{{{ inputs.{name} }}}}")
    return url


def _derive_outputs(spec: dict[str, Any], transcript: list[dict[str, Any]],
                    final_obs: Observation) -> list[OutputParam]:
    """Turn the model's reported outputs into extraction locators.

    The spec declares which outputs the business wants and their types. The
    model reports the values it read at finish(). The compiler locates each of
    those values in the final observation and records how to find it again --
    so replay re-reads the value from the screen instead of trusting a
    transcript.
    """
    reported: dict[str, Any] = {}
    for record in transcript:
        if record.get("tool") == "finish":
            reported = (record.get("args") or {}).get("outputs") or {}
    declared = spec.get("outputs", [])
    if not declared:
        return []
    if not reported:
        raise CompileError("discovery finished without reporting outputs")

    outputs: list[OutputParam] = []
    missing: list[str] = []
    for declaration in declared:
        name = declaration["name"]
        value = str(reported.get(name, "")).strip()
        locator = _locate_value(value, final_obs) if value else None
        if locator is None:
            missing.append(name)
            continue
        outputs.append(OutputParam(name=name, type=declaration["type"], extract=locator,
                                   description=declaration.get("description")))
    if missing:
        raise CompileError(
            "could not locate these declared outputs in the final observation: "
            + ", ".join(missing)
            + ". The model reported values that are not on the screen it ended on."
        )
    return outputs


def _locate_value(value: str, obs: Observation) -> Locator | None:
    for element in obs.elements:
        if element.name.strip() != value:
            continue
        if element.role == "cell" and element.row_key and element.column:
            return Locator(strategy="table_anchor", role="cell", header=element.column,
                           row_key=element.row_key, frame_path=list(element.frame_path))
        return Locator(strategy="ordinal", role=element.role, index=element.nth,
                       frame_path=list(element.frame_path))
    return None
