"""RawNode -> Element. PLATFORM-NEUTRAL (CLAUDE.md §4.10, §8.3).

Runs identically on every platform, so a future Windows adapter writes a tree
walk and inherits all of this.

Filtering is the judgment call that decides whether the agent works, and it is
squeezed from both sides:

  too many elements -> the overlay is unreadable AND each costs ~15 prompt
                       tokens; 300 elements is ~4,500 tokens of pure noise
  too few elements  -> the thing the agent needs to click has no number, so
                       it cannot be clicked at all

The first failure is a LATENCY failure. That is why perception belongs in a
project about latency and not in an appendix.
"""

import structlog

from os_agent.types import Element, RawNode

log = structlog.get_logger(__name__)

# Platform role -> our vocabulary. Normalizing HERE means prompts never leak
# platform dialect, and a model that works on macOS transfers to Windows.
ROLE_MAP: dict[str, str] = {
    "AXButton": "button",
    "AXPopUpButton": "dropdown",
    "AXMenuButton": "menubutton",
    "AXMenuItem": "menuitem",
    "AXMenuBarItem": "menu",
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXTextField": "textfield",
    "AXTextArea": "textarea",
    "AXSearchField": "searchfield",
    "AXComboBox": "combobox",
    "AXLink": "link",
    "AXTabButton": "tab",
    "AXRadioGroup": "tabgroup",
    "AXSlider": "slider",
    "AXIncrementor": "stepper",
    "AXCell": "cell",
    "AXRow": "row",
    "AXStaticText": "text",
    "AXImage": "image",
    "AXDisclosureTriangle": "disclosure",
    "AXColorWell": "colorwell",
}

# Roles worth a number even with no accessible name — they are actionable.
ALWAYS_INTERACTIVE = {
    "button", "dropdown", "menubutton", "menuitem", "checkbox", "radio",
    "textfield", "textarea", "searchfield", "combobox", "link", "tab", "menu",
    "slider", "stepper", "disclosure", "colorwell",
}

# Roles worth a number ONLY if they carry a name — an unnamed cell or label is
# decoration, and decoration is the thing that floods the overlay.
NAMED_ONLY = {"cell", "row", "text", "image"}

MIN_AREA = 20        # pt^2 — below this a box is not clickable anyway
ROW_BAND = 10        # pt — see _reading_order
SPARSE_THRESHOLD = 3


def normalize_role(platform_role: str) -> str:
    return ROLE_MAP.get(platform_role, platform_role.removeprefix("AX").lower())


def _keep(role: str, name: str, bbox: tuple[int, int, int, int] | None, enabled: bool) -> bool:
    if bbox is None or not enabled:
        return False
    x0, y0, x1, y1 = bbox
    if (x1 - x0) * (y1 - y0) < MIN_AREA:
        return False
    if role in ALWAYS_INTERACTIVE:
        return True
    if role in NAMED_ONLY:
        return bool(name)
    return False  # containers, groups, decoration


def _reading_order(items: list[tuple[str, str, tuple[int, int, int, int]]]):
    """Top-to-bottom, left-to-right — banded.

    Sorting on raw y0 jitters: two controls that look like one toolbar row can
    differ by 1-2 pt, which would interleave them with the row below and make
    the numbering incoherent. Quantizing y into ROW_BAND buckets first makes a
    visual row sort as a row, which is what lets the model reason "the toolbar
    is the low numbers".
    """
    return sorted(items, key=lambda it: (it[2][1] // ROW_BAND, it[2][0]))


def to_elements(nodes: list[RawNode], *, app: str = "") -> list[Element]:
    kept: list[tuple[str, str, tuple[int, int, int, int]]] = []
    seen: set[tuple[int, int, int, int]] = set()

    for n in nodes:
        role = normalize_role(n.role)
        if not _keep(role, n.name, n.bbox, n.enabled):
            continue
        # Toolkits wrap a control in a same-sized container; both report the
        # identical bbox and would get two numbers pointing at one pixel.
        if n.bbox in seen:
            continue
        seen.add(n.bbox)
        kept.append((role, n.name, n.bbox))

    elements = [
        Element(id=i, role=role, name=name, bbox=bbox)
        for i, (role, name, bbox) in enumerate(_reading_order(kept))
    ]

    if len(elements) < SPARSE_THRESHOLD and nodes:
        # The OCR trigger (CLAUDE.md §8.2). We instrument this rather than
        # pre-building the remedy: if it never fires across the suite, OCR was
        # never needed. If it does, we build it against the real failing app.
        log.warning(
            "perception.sparse",
            app=app,
            raw_nodes=len(nodes),
            elements=len(elements),
            roles=sorted({n.role for n in nodes})[:8],
        )

    return elements
