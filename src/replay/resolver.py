"""The ranked locator ladder.

Try each rung in order and record which one resolved. Exhausting the ladder is
a hard failure, never a guess: a capability that cannot find its control has no
business clicking something that looks similar.

Which rung resolved is itself the signal. A step recorded at rung 1 that now
resolves at rung 3 still works, but the surface has moved underneath it -- see
drift detection in the executor and cli.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.artifact.models import LADDER, Locator, LocatorStrategy, TargetSpec
from src.surface.playwright_driver import PlaywrightSurface

PROBE_TIMEOUT_MS = 1500
SETTLE_PROBE_MS = 600


@dataclass
class Resolved:
    rung: LocatorStrategy | None = None
    locator: Any | None = None
    bbox: tuple[int, int, int, int] | None = None
    attempted: list[LocatorStrategy] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.locator is not None or self.bbox is not None


def resolve(surface: PlaywrightSurface, target: TargetSpec,
            allow_bbox: bool = False) -> Resolved:
    """Walk the ladder. The primary is rung one; fallbacks follow in rung order.

    bbox is recorded by the compiler but not walked by default. Coordinates
    always "resolve" -- there is nothing to fail -- so leaving that rung in the
    automatic walk would mean a step whose control has vanished still clicks
    wherever the control used to be, and the run reports success. That is the
    guessing the ladder exists to prevent, so using it is an explicit opt-in
    and it warns into the trace.
    """
    result = Resolved()
    candidates = [target.primary, *target.fallbacks]
    for locator in sorted(candidates, key=lambda l: LADDER.index(l.strategy)):
        if locator.strategy == "bbox" and not allow_bbox:
            continue
        result.attempted.append(locator.strategy)
        try:
            found = _try(surface, locator)
        except Exception:
            found = None
        if found is not None:
            result.rung = locator.strategy
            if locator.strategy == "bbox":
                result.bbox = locator.bbox
            else:
                result.locator = found
            return result
    return result


def _try(surface: PlaywrightSurface, locator: Locator) -> Any | None:
    frame = surface.frame_for(locator.frame_path)

    if locator.strategy == "role_name":
        found = frame.get_by_role(locator.role, name=locator.name, exact=True)

    elif locator.strategy == "label_adjacent":
        # The label is the leading cell of the control's row. Find the row by
        # that cell, then take the control inside it.
        row = frame.get_by_role("row").filter(
            has=frame.get_by_role("cell", name=locator.label, exact=True))
        found = row.get_by_role(locator.role)

    elif locator.strategy == "table_anchor":
        return _table_anchor(frame, locator)

    elif locator.strategy == "ordinal":
        found = frame.get_by_role(locator.role).nth(locator.index or 0)

    elif locator.strategy == "bbox":
        return locator.bbox

    else:
        return None

    return found.first if _visible(found) else None


def _table_anchor(frame: Any, locator: Locator) -> Any | None:
    """Find the row whose cell matches row_key, then the cell under `header`.

    Legacy markup bolds a <td> rather than using <th>, so there are no real
    column headers to ask for. The first row is the header by convention, and
    the column index it yields is what addresses the cell.
    """
    rows = frame.get_by_role("row")
    count = rows.count()
    header_index: int | None = None
    for index in range(count):
        cells = rows.nth(index).get_by_role("cell")
        texts = [(cells.nth(i).inner_text() or "").strip() for i in range(cells.count())]
        if header_index is None and locator.header in texts:
            header_index = texts.index(locator.header)
            continue
        if header_index is not None and locator.row_key in texts:
            cell = cells.nth(header_index)
            return cell if _visible(cell) else None
    return None


def _visible(locator: Any) -> bool:
    """Is exactly this control on screen right now?

    The fast path is count(), so a rung that genuinely does not match costs
    nothing. A zero count still gets one short wait, because a frame swapped
    out by the previous action can report nothing for a moment and we would
    rather spend 600ms than drop to a lower rung for no reason.
    """
    try:
        if locator.count() > 0:
            return locator.first.is_visible(timeout=PROBE_TIMEOUT_MS)
        locator.first.wait_for(state="visible", timeout=SETTLE_PROBE_MS)
        return True
    except Exception:
        return False
