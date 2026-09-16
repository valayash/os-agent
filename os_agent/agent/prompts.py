"""Prompt builders — REBUILT every step, never appended (CLAUDE.md §4.2, §7).

There is no message history here and there never will be. Step 40 sends the
same shape and roughly the same size as step 1. That is the whole thesis, and
it lives in this file as an absence rather than a feature.

STATIC / VOLATILE SPLIT
Providers cache a PREFIX. Everything stable goes in the system message and
never moves; everything that changes goes in the user message. Put a timestamp
or a step counter in the system half and the cache is dead forever (§8.1).
"""

from os_agent.agent.state import FACTS_CAP, AgentState
from os_agent.types import Element

# ---------------------------------------------------------------------------
# STATIC. Byte-identical on every call, for the life of the run.
# ---------------------------------------------------------------------------
SYSTEM = """You operate a macOS desktop by looking at a screenshot and choosing ONE action.

The screenshot has numbered red boxes drawn on the interactive elements. The same
elements are listed as text below the goal. Refer to elements BY NUMBER.

ACTIONS
  click / double_click / right_click   need element_id
  type                                 needs text (types into whatever has focus)
  key                                  needs keys, e.g. ["cmd","s"] or ["enter"]
  scroll                               needs element_id and amount (negative = down)
  wait                                 amount in milliseconds
  done                                 the goal is achieved
  fail                                 the goal cannot be achieved

RULES
  One action per reply. The smallest step that makes progress.
  Prefer element_id over coords. Use coords only when nothing in the list fits.
  Menus work in two steps: click the menu, then the item appears next turn.
  If the goal is already satisfied by what you can see, emit done immediately.

EXPECTATION — required, and it is how your action gets verified without a second
model call. Predict the CHEAPEST observable consequence:
  element_appears     value = a substring of the new element's name
  element_disappears  value = a substring of the vanishing element's name
  text_in_element     element_id + value = substring the element should contain
  screen_changed      when you expect a visible change but nothing nameable
  none                only with done or fail

FACTS — new_fact records ONE durable discovery, or null. A fact is a property of
the world ("the file is at ~/notes.txt", "Save As is cmd+shift+s here"). It is
never a narration of what you just did ("clicked the File menu")."""


# ---------------------------------------------------------------------------
# VOLATILE. Rebuilt from state, every step.
# ---------------------------------------------------------------------------
def format_elements(elements: list[Element], limit: int = 80) -> str:
    """The text half of Set-of-Marks.

    The model gets both the picture and this list. The list carries the TRUTH:
    A2 found the number chip covers the first ~2 characters of an element's own
    label on screen, so names are read from here, never off the pixels (§8.4).
    """
    if not elements:
        return "(no interactive elements detected — the app may expose no accessibility tree)"
    rows = [
        f"[{e.id:>2}] {e.role:<11} {e.name[:52]!r}"
        for e in elements[:limit]
    ]
    if len(elements) > limit:
        rows.append(f"... {len(elements) - limit} more not shown")
    return "\n".join(rows)


def build_user(state: AgentState) -> str:
    """Everything the planner needs, and nothing it doesn't."""
    parts = [f"GOAL: {state['goal']}"]

    app = state.get("app") or "unknown"
    parts.append(f"APP:  {app}")

    if state.get("subgoal"):
        parts.append(f"WORKING ON: {state['subgoal']}")

    # What just happened — this replaces a message history entirely. One line.
    last_action = state.get("last_action")
    if last_action is not None:
        target = (f" element {last_action.element_id}"
                  if last_action.element_id is not None else "")
        detail = f" {last_action.text!r}" if last_action.text else ""
        keys = f" {last_action.keys}" if last_action.keys else ""
        parts.append(
            f"LAST: {last_action.kind}{target}{detail}{keys} -> "
            f"{state.get('last_outcome') or 'unknown'}"
        )
        if state.get("last_outcome") in ("no_change", "error"):
            why = state.get("last_reason") or ""
            parts.append(
                f"That did not work{f' ({why})' if why else ''}. "
                "Try a DIFFERENT approach — repeating it will not help."
            )

    facts = state.get("facts") or []
    if facts:
        parts.append("FACTS:\n" + "\n".join(f"  - {f}" for f in facts))
    parts.append(f"(facts are capped at {FACTS_CAP}; the oldest is dropped)")

    parts.append(f"\nELEMENTS\n{format_elements(state.get('elements') or [])}")
    parts.append(f"\nSTEP {state['step'] + 1}. Choose one action.")
    return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    """Rough, for budget logging only. ~4 chars per token."""
    return len(text) // 4
