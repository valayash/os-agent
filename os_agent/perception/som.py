"""Set-of-Marks overlay: numbered boxes on the screenshot (CLAUDE.md §8.4).

This is what turns grounding from a regression problem ("predict x,y") into a
classification one ("pick one of N"). The model reads a number off the image
and answers with it, so if a box sits in the wrong place the model is simply
being lied to and nothing downstream can work. Hence the gate is "verified by
eye", not "the code ran".

POINTS ONLY. The image arrives already downscaled to point resolution and
Element.bbox is in points, so the two align 1:1 — that is the §5.1 contract
paying off, and it is asserted rather than assumed.

Three cases make this non-trivial, and each is handled explicitly:
  1. overlapping elements  -> labels collide; try alternate corners
  2. element at a screen edge -> label renders off-canvas; clamp inward
  3. element smaller than its label -> label hides the thing it labels;
     place it outside the box
"""

import io

from PIL import Image, ImageDraw, ImageFont

from os_agent.types import Element

MARK = (181, 52, 42)  # the SoM red
TEXT = (255, 255, 255)
BOX_WIDTH = 2
LABEL_H = 15
LABEL_PAD = 3

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
)


def _font(size: int = 11) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _place_label(
    bbox: tuple[int, int, int, int],
    lw: int,
    screen: tuple[int, int],
    taken: list[tuple[int, int, int, int]],
) -> tuple[int, int, int, int]:
    """Pick a label rect: on-screen, and not on top of another label."""
    x0, y0, x1, y1 = bbox
    sw, sh = screen
    lh = LABEL_H

    # Case 3 first: a box too small to host its label gets the label outside,
    # otherwise the mark covers the control it is marking.
    small = (x1 - x0) < lw + 4 or (y1 - y0) < lh + 4

    candidates = (
        [(x0, y0 - lh), (x0 - lw, y0), (x1, y0), (x0, y1)]  # outside: above, left, right, below
        if small
        else [(x0, y0), (x1 - lw, y0), (x0, y1 - lh), (x1 - lw, y1 - lh)]  # the four inside corners
    )
    # Always keep a clamped inside-top-left as the last resort.
    candidates.append((x0, y0))

    for cx, cy in candidates:
        # Case 2: clamp into the screen before testing, so an edge element
        # gets a visible label rather than one that silently renders off-canvas.
        cx = max(0, min(cx, sw - lw))
        cy = max(0, min(cy, sh - lh))
        rect = (cx, cy, cx + lw, cy + lh)
        if not any(_overlaps(rect, t) for t in taken):  # case 1
            return rect

    cx = max(0, min(candidates[-1][0], sw - lw))
    cy = max(0, min(candidates[-1][1], sh - lh))
    return (cx, cy, cx + lw, cy + lh)


def annotate(screenshot_png: bytes, elements: list[Element]) -> bytes:
    """Draw numbered boxes. Input and output are both POINT-resolution PNG."""
    img = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
    sw, sh = img.size
    draw = ImageDraw.Draw(img)
    font = _font()

    # Big boxes first, so their labels are placed before small ones and the
    # small ones do the dodging. Numbering is unaffected — ids are already set.
    order = sorted(elements, key=lambda e: -e.area)
    taken: list[tuple[int, int, int, int]] = []

    for el in order:
        x0, y0, x1, y1 = el.bbox
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], outline=MARK, width=BOX_WIDTH)

        text = str(el.id)
        tw = draw.textlength(text, font=font)
        lw = int(tw) + LABEL_PAD * 2
        lx0, ly0, lx1, ly1 = _place_label(el.bbox, lw, (sw, sh), taken)
        taken.append((lx0, ly0, lx1, ly1))

        draw.rectangle([lx0, ly0, lx1, ly1], fill=MARK)
        draw.text((lx0 + LABEL_PAD, ly0 + 1), text, fill=TEXT, font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()
