"""Intervention requests.

The request carries enough context for an operator to act: which capability,
which step, why it stopped, where the live session is, and a screenshot.

It deliberately does not carry the input parameter values. A member id is
regulated data and an operator who needs it can read it off the screen they are
about to take control of. That omission is the redaction policy visibly costing
something, which is more convincing than a paragraph claiming we redact.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from src.guardrails.redaction import redact

REQUEST_FILENAME = "intervention.json"


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    capability_id: str
    version: int
    goal: str
    step_id: str
    risk: str
    reason: str
    url: str
    screenshot_path: str | None = None
    cdp_url: str = "http://localhost:9222"
    created_at: str = ""
    status: str = "open"
    # NOTE: input parameter VALUES are deliberately absent -- member PII.

    @classmethod
    def open(cls, **fields) -> "InterventionRequest":
        fields.setdefault("created_at",
                          datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return cls(**fields)


def write(request: InterventionRequest, run_dir: str | Path) -> Path:
    path = Path(run_dir) / REQUEST_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(redact(request.model_dump()), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(tmp, path)      # a half-written request must never be readable
    return path


def read(run_dir: str | Path) -> InterventionRequest | None:
    path = Path(run_dir) / REQUEST_FILENAME
    if not path.exists():
        return None
    return InterventionRequest(**json.loads(path.read_text()))


def open_requests(runs_dir: str | Path = "runs") -> list[InterventionRequest]:
    found = []
    for entry in sorted(Path(runs_dir).glob(f"*/{REQUEST_FILENAME}")):
        try:
            request = InterventionRequest(**json.loads(entry.read_text()))
        except Exception:
            continue
        found.append(request)
    return found


def set_status(run_dir: str | Path, status: str) -> None:
    request = read(run_dir)
    if request is None:
        return
    request.status = status
    write(request, run_dir)
