"""SEAM #3 — OS primitives.

An adapter exposes raw platform capability and NOTHING else: no filtering, no
numbering, no policy, no normalization (CLAUDE.md §4.10, §11). Everything an
adapter returns is dumb; the smart parts live one layer up and are shared
across platforms. That is the whole reason a future Windows adapter is a tree
walk rather than a second perception stack.

COORDINATES: every coordinate crossing this interface is in POINTS. Pixels
exist inside capture() and are gone by the time it returns (CLAUDE.md §5.1).
"""

from typing import Protocol, runtime_checkable

from os_agent.types import RawNode


@runtime_checkable
class DesktopAdapter(Protocol):
    """What the agent needs from an operating system."""

    name: str

    def screen_size_points(self) -> tuple[int, int]:
        """Logical screen size. The coordinate space everything else uses."""
        ...

    def capture(self) -> tuple[bytes, float]:
        """Return (PNG at POINT resolution, measured capture:point ratio).

        The ratio is RETURNED, not computed by the caller and never hardcoded:
        it depends on the user's current display setting and changes when they
        change resolution (CLAUDE.md §5.1).
        """
        ...

    def frontmost_app(self) -> tuple[str, str]:
        """(display_name, bundle_id). bundle_id is the policy allowlist key."""
        ...

    def raw_tree(self) -> list[RawNode]:
        """Unfiltered accessibility nodes, platform roles intact."""
        ...

    def click(self, x: int, y: int, kind: str = "click") -> None: ...
    def type_text(self, text: str) -> None: ...
    def press_keys(self, keys: list[str]) -> None: ...
    def scroll(self, x: int, y: int, amount: int) -> None: ...
    def ocr(self, image_png: bytes, bbox: tuple[int, int, int, int]) -> str: ...


class NotOnThisPlatform(NotImplementedError):
    """Raised by an unimplemented adapter. Explicit beats a silent wrong answer."""
