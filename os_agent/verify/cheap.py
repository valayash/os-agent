"""Did that work? Answered WITHOUT a model call (CLAUDE.md §8.7).

This file is the reason llm_calls/steps can be 1.00. The planner already told
us what it expected to happen; this just checks whether it did. Most agents ask
a model "did that work?" — that second call is what doubles the bill and the
latency, and §1 says it is the number that decides the project.

Four outcomes, never a bool (§4.4):
    success    the expectation was met
    no_change  nothing happened at all — the click missed, the key was eaten
    error      the executor raised, or an error dialog is on screen
    ambiguous  we genuinely could not tell  <- the only one that costs a model call

`ambiguous_rate` is a headline metric precisely because each one is a call we
failed to avoid.
"""

import io

import structlog
from PIL import Image, ImageChops

from os_agent.types import Element, Expectation, Outcome

log = structlog.get_logger(__name__)

# Below this fraction of changed pixels we call the screen unchanged. Cursor
# blink and clock ticks move a handful of pixels on an idle screen.
PIXEL_CHANGE_THRESHOLD = 0.001  # 0.1%
GREY_TOLERANCE = 12  # per-pixel difference below this is noise, not change

ERROR_WORDS = ("error", "failed", "cannot", "can't", "unable", "denied",
               "not permitted", "invalid", "warning")


def pixel_change_ratio(before_png: bytes, after_png: bytes) -> float:
    """Fraction of pixels that meaningfully changed. ~5 ms.

    Uses a histogram rather than a per-pixel loop — the histogram is computed in
    C, a Python loop over 1.3M pixels is not.
    """
    if not before_png or not after_png:
        return 1.0
    a = Image.open(io.BytesIO(before_png)).convert("L")
    b = Image.open(io.BytesIO(after_png)).convert("L")
    if a.size != b.size:
        return 1.0  # the window resized; that is definitely a change
    hist = ImageChops.difference(a, b).histogram()
    changed = sum(hist[GREY_TOLERANCE:])
    total = a.size[0] * a.size[1]
    return changed / total if total else 0.0


def _names(elements: list[Element]) -> set[str]:
    return {e.name for e in elements if e.name}


def _has(elements: list[Element], needle: str) -> bool:
    n = needle.lower()
    return any(n in e.name.lower() for e in elements)


def error_dialog(elements: list[Element]) -> str | None:
    """An error visible on screen means the action failed, whatever it changed."""
    for e in elements:
        low = e.name.lower()
        if any(w in low for w in ERROR_WORDS):
            return e.name[:80]
    return None


def check(
    expect: Expectation | None,
    *,
    before_elements: list[Element],
    after_elements: list[Element],
    before_png: bytes = b"",
    after_png: bytes = b"",
    executor_error: str | None = None,
) -> tuple[Outcome, str]:
    """Returns (outcome, one-line reason). The reason goes in the trajectory."""

    # 1. the executor already knows it failed. Nothing else matters.
    if executor_error:
        return "error", f"executor raised: {executor_error[:80]}"

    # 2. an error dialog on screen beats any expectation being "met"
    if (msg := error_dialog(after_elements)) and not error_dialog(before_elements):
        return "error", f"error dialog appeared: {msg!r}"

    changed = pixel_change_ratio(before_png, after_png)
    before_names, after_names = _names(before_elements), _names(after_elements)
    tree_moved = before_names != after_names
    anything_happened = changed > PIXEL_CHANGE_THRESHOLD or tree_moved

    # 3. nothing at all happened — the click missed or the key was swallowed.
    #    This is checked BEFORE the expectation, because an expectation that
    #    "passes" on a frozen screen is a false positive.
    if not anything_happened:
        return "no_change", f"screen identical ({changed:.4%} pixels, same element set)"

    if expect is None or expect.kind == "none":
        # The planner declined to predict. We can still say something happened,
        # which is weak but honest and costs nothing.
        return "success", "no expectation given; screen changed"

    kind, value = expect.kind, (expect.value or "")

    if kind == "element_appears":
        if not value:
            return "ambiguous", "element_appears with no value to match"
        if _has(after_elements, value) and not _has(before_elements, value):
            return "success", f"{value!r} appeared"
        if _has(after_elements, value):
            return "ambiguous", f"{value!r} was already present before the action"
        return "no_change", f"{value!r} did not appear"

    if kind == "element_disappears":
        if not value:
            return "ambiguous", "element_disappears with no value to match"
        if _has(before_elements, value) and not _has(after_elements, value):
            return "success", f"{value!r} disappeared"
        if not _has(before_elements, value):
            return "ambiguous", f"{value!r} was not there to begin with"
        return "no_change", f"{value!r} is still present"

    if kind == "text_in_element":
        if expect.element_id is None or not value:
            return "ambiguous", "text_in_element needs both element_id and value"
        target = next((e for e in after_elements if e.id == expect.element_id), None)
        if target is None:
            return "ambiguous", f"element {expect.element_id} is gone; cannot read it"
        if value.lower() in target.name.lower():
            return "success", f"element {expect.element_id} contains {value!r}"
        return "no_change", f"element {expect.element_id} does not contain {value!r}"

    if kind == "screen_changed":
        return "success", f"screen changed ({changed:.2%} pixels)"

    return "ambiguous", f"unknown expectation kind {kind!r}"
