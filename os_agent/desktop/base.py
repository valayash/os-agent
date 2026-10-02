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

from os_agent.types import RawNode, Receipt


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

    # -- AX execution path (EXEC_MODE=ax, CLAUDE.md §4.11) -------------------
    # Sent to the target PROCESS, not through the window server: no cursor, no
    # keystroke, no focus change. `path` is the opaque handle the same adapter
    # put on RawNode.path; nothing above this layer parses it.
    #
    # The executor GUARDS every call: read_node(path) first, refuse if the node
    # no longer has the role and name the planner saw. Positional handles go
    # stale exactly like positional ids do (§4.7).

    def read_node(self, path: str) -> RawNode | None:
        """Re-resolve a handle and read the node fresh. None if it is gone."""
        ...

    def read_value(self, path: str) -> str | None:
        """The node's current value, for read-back after set_value."""
        ...

    def press(self, path: str) -> Receipt: ...
    def set_value(self, path: str, text: str) -> Receipt: ...

    def menu_tree(self) -> list[RawNode]:
        """Menu-bar items of the target app, name = full title path
        ("Format > Make Rich Text"). Unfiltered; perception decides."""
        ...

    def invoke_menu(self, path: str) -> Receipt: ...


class NotOnThisPlatform(NotImplementedError):
    """Raised by an unimplemented adapter. Explicit beats a silent wrong answer."""
