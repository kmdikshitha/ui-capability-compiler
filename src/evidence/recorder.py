"""Run directories, step traces, screenshots, tracing.

Every writer here goes through redact(). That is the whole point of funnelling
evidence through one module: there is no second path to disk that could forget.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.artifact.models import canonical_json
from src.guardrails.redaction import redact

RUNS_DIR = Path("runs")


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"


class Recorder:
    """One run directory, one recorder."""

    def __init__(self, run_id: str, base: str | Path = RUNS_DIR) -> None:
        self.run_id = run_id
        self.dir = Path(base) / run_id
        self.screenshots = self.dir / "screenshots"
        self.screenshots.mkdir(parents=True, exist_ok=True)

    # -- paths -------------------------------------------------------------

    def path(self, name: str) -> Path:
        return self.dir / name

    def screenshot_path(self, label: str | int) -> str:
        return str(self.screenshots / f"{label}.png")

    @property
    def trace_path(self) -> str:
        return str(self.dir / "trace.zip")

    # -- writers -----------------------------------------------------------

    def meta(self, **fields: Any) -> None:
        self._started = time.monotonic()
        self._write_json("meta.json", {
            "run_id": self.run_id,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **fields})

    def finish(self, **fields: Any) -> None:
        """Close out meta.json with a duration, so timing claims stay checkable."""
        path = self.dir / "meta.json"
        payload = json.loads(path.read_text()) if path.exists() else {"run_id": self.run_id}
        payload["ended_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if getattr(self, "_started", None) is not None:
            payload["duration_s"] = round(time.monotonic() - self._started, 1)
        payload.update(fields)
        self._write_json("meta.json", payload)

    def result(self, payload: dict[str, Any]) -> None:
        self._write_json("result.json", payload)

    def append(self, filename: str, record: dict[str, Any]) -> None:
        """Append one redacted JSON line."""
        path = self.dir / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(redact(record)) + "\n")

    def transcript(self, record: dict[str, Any]) -> None:
        self.append("transcript.jsonl", record)

    def step(self, record: dict[str, Any]) -> None:
        self.append("steps.jsonl", record)

    def control(self, record: dict[str, Any]) -> None:
        """Control transfers. This file is the transport, not just a log."""
        self.append("control.jsonl", record)

    def _write_json(self, filename: str, payload: dict[str, Any]) -> None:
        path = self.dir / filename
        body = json.dumps(redact(payload), indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text().splitlines() if line.strip()]
