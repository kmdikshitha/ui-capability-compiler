"""ARIA snapshot -> Observation.

Playwright emits the accessibility tree as an indented YAML-ish document. This
module parses it and flattens it into typed Elements. It imports nothing from
playwright on purpose: everything here is string processing over an
accessibility tree, which is the same shape a desktop AX API would hand us.

The interesting work is filling in what the tree leaves blank. A legacy form
renders its label as the leading cell of a table row:

    - row "Initial Deposit":
      - cell "Initial Deposit"
      - cell:
        - textbox            <- no accessible name at all

The control has no name, but the tree still says what it is, because the row
says so. We derive the label from the row and mark name_source="adjacent" so
the compiler knows to emit a label_adjacent locator rather than role_name.
"""

from __future__ import annotations

import re

from src.types import INTERACTIVE_ROLES, KNOWN_ROLES, Element, Observation

MAX_DIGEST_CHARS = 1800

_LINE = re.compile(
    r'^(?P<role>[a-zA-Z][\w-]*)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?P<attrs>(?:\s*\[[^\]]*\])*)\s*$'
)
# A control carrying a value renders as `textbox "Member ID": 12345`.
_LINE_VALUE = re.compile(
    r'^(?P<role>[a-zA-Z][\w-]*)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?P<attrs>(?:\s*\[[^\]]*\])*)'
    r'\s*:\s*(?P<value>.*)$'
)
_ATTR = re.compile(r'\[([^\]=]+)(?:=([^\]]*))?\]')


class Node:
    """One entry in the accessibility tree."""

    __slots__ = ("role", "name", "attrs", "children", "parent")

    def __init__(self, role: str, name: str = "", attrs: dict[str, str] | None = None):
        self.role = role
        self.name = name
        self.attrs: dict[str, str] = attrs or {}
        self.children: list[Node] = []
        self.parent: Node | None = None

    def add(self, child: "Node") -> None:
        child.parent = self
        self.children.append(child)

    def ancestor(self, role: str) -> "Node | None":
        node = self.parent
        while node is not None:
            if node.role == role:
                return node
            node = node.parent
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Node({self.role!r}, {self.name!r}, {len(self.children)} children)"


def parse_aria_snapshot(text: str) -> Node:
    """Parse an indented ARIA snapshot into a tree rooted at a synthetic node.

    Indentation-driven rather than YAML-parsed: accessible names come from page
    content and routinely contain colons, quotes and dashes that make a strict
    YAML load fail on pages this module exists to handle.
    """
    root = Node("root")
    stack: list[tuple[int, Node]] = [(-1, root)]

    for raw in text.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.lstrip(" ")
        if not content.startswith("- "):
            continue
        content = content[2:].strip()
        has_children = content.endswith(":")
        if has_children:
            content = content[:-1].rstrip()

        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root

        # Metadata lines such as `/url: /members/12345` belong to the parent.
        if content.startswith("/"):
            key, _, value = content.partition(":")
            parent.attrs[key.strip("/")] = value.strip()
            continue

        # Free text nodes arrive as `text: the visible string`.
        if content.startswith("text:"):
            node = Node("text", content[5:].strip())
            parent.add(node)
            stack.append((indent, node))
            continue

        match = _LINE.match(content) or _LINE_VALUE.match(content)
        if match is None:
            continue
        role = match.group("role")
        name = (match.group("name") or "").replace('\\"', '"')
        attrs = {k.strip(): (v.strip() if v is not None else "true")
                 for k, v in _ATTR.findall(match.group("attrs") or "")}
        if "value" in match.groupdict() and match.group("value"):
            attrs["value"] = match.group("value").strip()
        node = Node(role if role in KNOWN_ROLES else "generic", name, attrs)
        parent.add(node)
        stack.append((indent, node))

    return root


def _row_label(node: Node) -> str:
    """The leading cell of the row that contains this control, if any.

    This is the adjacency rule, expressed against the accessibility tree rather
    than the DOM: in a table-laid-out legacy form, the label for a control is
    the text of the first cell of its row.
    """
    row = node.ancestor("row")
    if row is None:
        return ""
    for cell in row.children:
        if cell.role == "cell" and cell.name.strip():
            return cell.name.strip()
    return ""


def _column_headers(table: Node) -> list[str]:
    """Header texts of a table's first row.

    Legacy markup bolds a <td> instead of using <th>, so the accessibility tree
    reports no column headers. The first row is the convention these apps
    follow, and table_anchor targeting needs it.
    """
    for group in (table, *[c for c in table.children if c.role == "rowgroup"]):
        for row in group.children:
            if row.role == "row":
                return [c.name.strip() for c in row.children if c.role == "cell"]
    return []


# Structural roles that carry no targeting value and would only bloat the
# observation handed to the model.
_SKIP = frozenset({"root", "rowgroup", "generic", "separator"})


def flatten(root: Node, frame_path: list[str], counters: dict[str, int],
            start_index: int) -> list[Element]:
    """Walk one frame's tree in document order and emit Elements."""
    elements: list[Element] = []
    index = start_index

    def visit(node: Node) -> None:
        nonlocal index
        if node.role not in _SKIP:
            # nth must count every element of this role in the frame, whether or
            # not we keep it, so it stays aligned with get_by_role(...).nth(i).
            nth = counters.get(node.role, 0)
            counters[node.role] = nth + 1

            name, source = node.name.strip(), "aria"
            if not name and node.role in INTERACTIVE_ROLES:
                adjacent = _row_label(node)
                name, source = (adjacent, "adjacent") if adjacent else ("", "none")
            elif not name:
                source = "none"

            row_key = column = None
            column_index = None
            if node.role == "cell":
                row = node.ancestor("row")
                table = node.ancestor("table")
                if row is not None:
                    cells = [c for c in row.children if c.role == "cell"]
                    if node in cells:
                        column_index = cells.index(node)
                    row_key = next((c.name.strip() for c in cells if c.name.strip()), None)
                    if table is not None and column_index is not None:
                        headers = _column_headers(table)
                        if column_index < len(headers):
                            column = headers[column_index] or None

            index += 1
            elements.append(Element(
                ref=f"e{index}",
                role=node.role,  # type: ignore[arg-type]
                name=name,
                name_source=source,  # type: ignore[arg-type]
                value=node.attrs.get("value"),
                row_key=row_key,
                column=column,
                column_index=column_index,
                nth=nth,
                frame_path=list(frame_path),
                enabled="disabled" not in node.attrs,
            ))
        for child in node.children:
            visit(child)

    for child in root.children:
        visit(child)
    return elements


def build_observation(url: str, title: str,
                      frames: list[tuple[list[str], str]],
                      texts: list[str],
                      screenshot_path: str | None = None) -> Observation:
    """Assemble one Observation from per-frame snapshots.

    `frames` is [(frame_path, aria_snapshot_text)], outermost first. Refs are
    assigned across the whole observation so a single ref identifies both the
    frame and the element within it.
    """
    elements: list[Element] = []
    for frame_path, snapshot in frames:
        root = parse_aria_snapshot(snapshot)
        counters: dict[str, int] = {}
        elements.extend(flatten(root, frame_path, counters, len(elements)))

    digest = " | ".join(t.strip().replace("\n", " ") for t in texts if t.strip())
    digest = re.sub(r"\s+", " ", digest)[:MAX_DIGEST_CHARS]

    return Observation(url=url, title=title, elements=elements,
                       text_digest=digest, screenshot_path=screenshot_path)
