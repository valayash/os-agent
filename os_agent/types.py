"""Core data contracts.

See CLAUDE.md §7. Do not change anything here without updating that file.

COORDINATE SPACE: every bbox and every coordinate in this module is in POINTS,
never pixels (CLAUDE.md §5.1). Pixels exist inside DesktopAdapter.capture() and
nowhere else.
"""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Action space
# --------------------------------------------------------------------------
# Deliberately absent: "launch", "shell". Launching apps and touching the
# filesystem are TaskSpec.setup()'s job — trusted project code. The agent's
# action space contains no shell, ever (CLAUDE.md §8.6).
ActionKind = Literal[
    "click", "double_click", "right_click",
    "type", "key", "scroll", "drag",
    "wait", "done", "fail",
]

# Verification result. Four values, not a bool — `ambiguous` is the one that
# costs an LLM call, and driving its rate down is a Phase B target (§4.4).
Outcome = Literal["success", "no_change", "error", "ambiguous"]


# --------------------------------------------------------------------------
# Perception
# --------------------------------------------------------------------------
@dataclass(slots=True)
class RawNode:
    """One unfiltered accessibility node, exactly as the platform reports it.

    Adapters emit these; perception/elements.py turns them into Elements.
    `role` is the PLATFORM role ("AXButton", "Button") — normalization happens
    in perception, not in the adapter (CLAUDE.md §4.10), so a future Windows
    adapter writes a tree walk and not a second perception stack.

    bbox may be None: plenty of real nodes have no geometry, and dropping them
    is a filtering decision, which is perception's call and not the adapter's.
    """

    role: str
    name: str
    bbox: tuple[int, int, int, int] | None
    enabled: bool = True
    focused: bool = False
    depth: int = 0


@dataclass(slots=True)
class Element:
    """An interactive element the model can reference by number."""

    id: int  # POSITIONAL — renumbered every observation. See .key() below.
    role: str  # normalized: "button", "textfield", "menuitem", ...
    name: str
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 — POINTS
    enabled: bool = True

    @property
    def center(self) -> tuple[int, int]:
        """Click target. The executor resolves element_id through here."""
        x0, y0, x1, y1 = self.bbox
        return (x0 + x1) // 2, (y0 + y1) // 2

    @property
    def area(self) -> int:
        x0, y0, x1, y1 = self.bbox
        return max(0, x1 - x0) * max(0, y1 - y0)

    def key(self) -> str:
        """Stable identity for trajectories (CLAUDE.md §4.7).

        `id` is positional, so a recorded "click element 12" is meaningless on
        replay — one extra element shifts every number. Trajectories record
        this alongside the id so a past run can be re-grounded later.
        """
        return f"{self.role}|{self.name}|{self.bbox}"


@dataclass(slots=True)
class Observation:
    """One captured screen. Everything the planner sees, except the goal."""

    screenshot: bytes  # PNG, already downscaled to POINT resolution
    annotated: bytes  # PNG with the SoM overlay drawn on
    elements: list[Element]
    meta: dict = field(default_factory=dict)  # app, bundle_id, title, scale


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
@dataclass
class Action:
    """One thing to do. Lists of these, never singles (CLAUDE.md §4.1)."""

    kind: ActionKind
    element_id: int | None = None
    text: str | None = None
    keys: list[str] | None = None
    amount: int | None = None  # scroll ticks
    # Escape hatch for when perception misses a target. POINTS.
    # NOTE: this serializes as a JSON array-of-2 in the planner's schema.
    # Verify at A4 that the provider's structured output handles fixed-length
    # tuples; if not, this becomes two fields rather than a silent coercion.
    coords: tuple[int, int] | None = None

    def is_terminal(self) -> bool:
        """done/fail never reach the executor — the graph ends on them."""
        return self.kind in ("done", "fail")


# --------------------------------------------------------------------------
# LLM boundary — Pydantic lives here and nowhere else (CLAUDE.md §10)
# --------------------------------------------------------------------------
class Expectation(BaseModel):
    """A machine-checkable postcondition, emitted with the action it predicts.

    This is what keeps verification off the LLM (CLAUDE.md §4.5). Without it
    the verifier can only distinguish "something changed" from "nothing
    changed", every real outcome lands on `ambiguous`, and calls-per-step
    climbs above 1.
    """

    kind: Literal[
        "element_appears",
        "element_disappears",
        "text_in_element",
        "screen_changed",
        "none",
    ]
    value: str | None = Field(
        default=None, description="Substring to match against an element name or text."
    )
    element_id: int | None = Field(
        default=None, description="Element to inspect, for text_in_element."
    )


class PlannedAction(BaseModel):
    """The planner's entire output. One call per step produces all of this."""

    reasoning: str = Field(description="At most two sentences. Why this action, now.")
    subgoal: str = Field(description="The immediate objective, a few words.")
    action: Action
    expect: Expectation
    confidence: float = Field(ge=0.0, le=1.0, description="0-1. Unused in Phase A.")
    new_fact: str | None = Field(
        default=None,
        description=(
            "At most ONE durable discovery worth remembering, or null. "
            "A fact is a property of the world ('the file is at ~/x.odt'), "
            "never a narration of what you just did ('clicked the File menu')."
        ),
    )
