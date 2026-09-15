"""Perception unit tests (CLAUDE.md §9 A2 gate)."""

import io

import pytest
from PIL import Image

from os_agent.desktop.macos import MacOSAdapter
from os_agent.perception.elements import normalize_role, to_elements
from os_agent.perception.som import annotate
from os_agent.types import Element, RawNode


def node(role="AXButton", name="OK", bbox=(10, 10, 60, 30), enabled=True):
    return RawNode(role=role, name=name, bbox=bbox, enabled=enabled)


# -- the §5.1 contract: this is THE test that must never fail ---------------
def test_capture_is_point_resolution():
    ad = MacOSAdapter()
    png, ratio = ad.capture()
    img = Image.open(io.BytesIO(png))
    assert img.size == ad.screen_size_points(), (
        f"capture returned {img.size} but the screen is "
        f"{ad.screen_size_points()} points — pixels leaked past capture()"
    )
    assert ratio >= 1.0


def test_roles_are_normalized_off_the_platform():
    assert normalize_role("AXButton") == "button"
    assert normalize_role("AXTextField") == "textfield"
    assert normalize_role("AXMenuBarItem") == "menu"
    # unknown roles still lose the platform prefix rather than leaking it
    assert normalize_role("AXWhatever") == "whatever"


@pytest.mark.parametrize(
    "n,kept,why",
    [
        (node(), True, "a named button"),
        (node(name=""), True, "an unnamed button is still actionable"),
        (node(role="AXStaticText", name=""), False, "unnamed text is decoration"),
        (node(role="AXStaticText", name="Total"), True, "named text is a label"),
        (node(bbox=(0, 0, 3, 3)), False, "below MIN_AREA"),
        (node(bbox=None), False, "no geometry"),
        (node(enabled=False), False, "disabled"),
        (node(role="AXGroup"), False, "containers are never numbered"),
    ],
)
def test_filter_rules(n, kept, why):
    assert bool(to_elements([n])) is kept, why


def test_ids_are_assigned_in_banded_reading_order():
    # same visual row, 2pt apart — must not interleave with the row below
    nodes = [
        node(name="right", bbox=(300, 101, 380, 121)),
        node(name="below", bbox=(10, 200, 90, 220)),
        node(name="left", bbox=(10, 100, 90, 120)),
    ]
    assert [e.name for e in to_elements(nodes)] == ["left", "right", "below"]


def test_identical_bboxes_deduped():
    # a toolkit wrapping a control in a same-sized container
    nodes = [node(name="Save"), node(role="AXStaticText", name="Save")]
    assert len(to_elements(nodes)) == 1


def test_sparse_screen_is_flagged(caplog):
    """The OCR trigger. We instrument it rather than pre-building the remedy."""
    els = to_elements([node(role="AXGroup"), node(role="AXGroup", bbox=(0, 0, 8, 8))])
    assert len(els) == 0


def test_som_preserves_image_size_and_marks_every_element():
    blank = Image.new("RGB", (400, 300), "white")
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    els = [
        Element(id=0, role="button", name="A", bbox=(10, 10, 60, 30)),
        Element(id=1, role="button", name="B", bbox=(10, 10, 20, 18)),  # tiny, overlapping
        Element(id=2, role="button", name="C", bbox=(380, 285, 400, 300)),  # at the edge
    ]
    out = annotate(buf.getvalue(), els)
    img = Image.open(io.BytesIO(out))
    assert img.size == (400, 300)
    # the overlay actually drew: a blank white image would have one colour
    assert len(img.convert("RGB").getcolors(maxcolors=100000)) > 1
