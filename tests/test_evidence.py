"""Nothing committed under evidence/ may carry seeded member data.

This encodes a definition-of-done item mechanically. An earlier version of the
check grepped text files only and skipped anything binary -- which is how six
Playwright traces full of member ids and balances got past it. Archives are now
opened and scanned member by member.

Screenshots are the one thing this cannot see: redaction operates on text, and
images are pixels. That limit is stated in REPORT.md §6, and evidence generation
only keeps screenshots of screens that stop before member data is shown.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from mock_app.data import MEMBERS

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"
IMAGES = {".png", ".jpg", ".jpeg", ".webp"}


def _ids_and_balances() -> list[tuple[str, re.Pattern[bytes]]]:
    """What the definition of done names: raw member ids and balances."""
    found: list[tuple[str, re.Pattern[bytes]]] = []
    for member_id, record in {**MEMBERS, "99999": {"accounts": []}}.items():
        found.append((f"member id {member_id}",
                      re.compile(rb"(?<!\d)" + member_id.encode() + rb"(?!\d)")))
        for account in record["accounts"]:
            # Comma-grouped balances are distinctive; bare ones like 0.00 are not.
            if "," in account["balance"]:
                found.append((f"balance {account['balance']}",
                              re.compile(re.escape(account["balance"]).encode())))
    return found


def _names() -> list[tuple[str, re.Pattern[bytes]]]:
    return [(f"name {r['name']}", re.compile(re.escape(r["name"]).encode()))
            for r in MEMBERS.values() if r.get("name")]


def _scan(needles: list[tuple[str, re.Pattern[bytes]]]) -> list[str]:
    return [f"{where}: {label}" for where, blob in _blobs()
            for label, pattern in needles if pattern.search(blob)]


def _blobs():
    """Every scannable byte string under evidence/, archives opened."""
    for path in sorted(EVIDENCE.rglob("*")):
        if not path.is_file() or path.suffix.lower() in IMAGES:
            continue
        rel = path.relative_to(EVIDENCE)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for member in archive.namelist():
                    if Path(member).suffix.lower() in IMAGES:
                        continue
                    yield f"{rel}!{member}", archive.read(member)
        else:
            yield str(rel), path.read_bytes()


@pytest.mark.skipif(not EVIDENCE.exists(), reason="no evidence/ directory")
def test_no_member_ids_or_balances_in_committed_evidence() -> None:
    leaks = _scan(_ids_and_balances())
    assert not leaks, "member data in committed evidence:\n  " + "\n  ".join(leaks[:25])


@pytest.mark.skipif(not EVIDENCE.exists(), reason="no evidence/ directory")
@pytest.mark.xfail(strict=True, reason=(
    "Known limitation, REPORT.md §6: redaction masks values the contract declares "
    "sensitive. A member's name is page content, not an input, so nothing marks it "
    "as a name and it survives into transcripts and failure reports. Kept as an "
    "expected failure so the gap stays visible -- if it ever passes, strict=True "
    "turns that into a failure and the limitation can be struck from the report."))
def test_no_member_names_in_committed_evidence() -> None:
    leaks = _scan(_names())
    assert not leaks, "member names in committed evidence:\n  " + "\n  ".join(leaks)


@pytest.mark.skipif(not EVIDENCE.exists(), reason="no evidence/ directory")
def test_no_playwright_trace_is_committed() -> None:
    """Traces bypass redact() by construction; they belong under runs/ only."""
    traces = sorted(str(p.relative_to(EVIDENCE)) for p in EVIDENCE.rglob("trace.zip"))
    assert not traces, f"traces must not be committed: {traces}"
