"""Load, save and version capability artifacts.

Artifacts are immutable. A re-record or a learned fix writes v(n+1) and both
stay on disk, because something in production is calling v1 and a silent edit
under it is how you break a caller you cannot see.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from src.artifact.models import CapabilityArtifact, canonical_json
from src.guardrails.redaction import redact

ARTIFACT_DIR = Path("artifacts")
_FILENAME = re.compile(r"^(?P<cap>.+)\.v(?P<version>\d+)\.json$")


def artifact_path(capability_id: str, version: int, directory: Path | str = ARTIFACT_DIR) -> Path:
    return Path(directory) / f"{capability_id}.v{version}.json"


def content_hash(artifact: CapabilityArtifact) -> str:
    """Hash of the whole artifact, contract and mechanics alike."""
    return hashlib.sha256(
        canonical_json(artifact.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def save(artifact: CapabilityArtifact, directory: Path | str = ARTIFACT_DIR) -> Path:
    """Write canonical JSON atomically. Refuses to overwrite an existing version."""
    artifact.with_contract_hash()
    path = artifact_path(artifact.capability_id, artifact.version, directory)
    if path.exists():
        raise FileExistsError(
            f"{path} already exists; artifacts are immutable, write v{artifact.version + 1}"
        )
    return _write(artifact, path)


def save_status_change(artifact: CapabilityArtifact,
                       directory: Path | str = ARTIFACT_DIR) -> Path:
    """Persist a draft -> approved promotion in place.

    status is orthogonal to version: promoting a capability is a review
    decision about an existing recording, not a new recording, so it is the one
    field allowed to change without a version bump.
    """
    path = artifact_path(artifact.capability_id, artifact.version, directory)
    return _write(artifact, path)


def _write(artifact: CapabilityArtifact, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(redact(artifact.model_dump(mode="json")))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(tmp, path)          # write-temp-then-rename: no half-written artifact
    return path


def load(path: str | Path) -> CapabilityArtifact:
    return CapabilityArtifact(**json.loads(Path(path).read_text(encoding="utf-8")))


def versions(capability_id: str, directory: Path | str = ARTIFACT_DIR) -> list[int]:
    found: list[int] = []
    for entry in Path(directory).glob(f"{capability_id}.v*.json"):
        match = _FILENAME.match(entry.name)
        if match and match.group("cap") == capability_id:
            found.append(int(match.group("version")))
    return sorted(found)


def next_version(capability_id: str, directory: Path | str = ARTIFACT_DIR) -> int:
    existing = versions(capability_id, directory)
    return (existing[-1] + 1) if existing else 1


def load_latest(capability_id: str, directory: Path | str = ARTIFACT_DIR) -> CapabilityArtifact:
    existing = versions(capability_id, directory)
    if not existing:
        raise FileNotFoundError(f"no artifact for capability {capability_id!r}")
    return load(artifact_path(capability_id, existing[-1], directory))
