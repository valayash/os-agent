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


REFLECT_SYSTEM = """You are debugging a stuck computer-use agent.

It has repeated an action without progress, or failed twice in a row. You will see
the goal, what it tried, what the verifier said, and what is currently on screen.

Reply with ONE short instruction — two sentences at most — telling it what to do
DIFFERENTLY. Be concrete and name elements by number where you can.

Good:  "cmd+s is being swallowed. Open the File menu (element 2) and click Save."
Good:  "Element 13 is a label, not the text area. Click element 15 instead."
Bad:   "Try again."                      (it already did)
Bad:   "Make sure the file is saved."    (that is the goal, not a method)

You are NOT choosing the action. You are correcting the approach."""


def build_reflect_user(state: AgentState) -> str:
    """Fuller context than the planner gets — but still fixed-size.

    Reflection is allowed a richer prompt because it is rare. It is NOT allowed
    a history: that would reintroduce the growth §4.2 exists to prevent.
    """
    last = state.get("last_action")
    tried = "nothing yet"
    if last is not None:
        tried = (f"{last.kind}"
                 f"{f' element {last.element_id}' if last.element_id is not None else ''}"
                 f"{f' {last.text!r}' if last.text else ''}"
                 f"{f' {last.keys}' if last.keys else ''}")
    return "\n".join([
        f"GOAL: {state['goal']}",
        f"APP:  {state.get('app') or 'unknown'}",
        f"IT IS STUCK AFTER {state['step']} STEPS.",
        f"consecutive failures: {state.get('consecutive_failures', 0)}"
        f"   repeating itself: {bool(state.get('looping'))}",
        f"LAST TRIED: {tried}",
        f"VERIFIER SAID: {state.get('last_outcome')} — {state.get('last_reason') or ''}",
        (f"PREVIOUS ADVICE (it did not work): {state['reflection_note']}"
         if state.get("reflection_note") else ""),
        "",
        f"ON SCREEN NOW\n{format_elements(state.get('elements') or [], limit=60)}",
        "",
        "What should it do differently?",
    ])


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

    # A correction from reflect, if it has fired. One slot, not a history.
    if state.get("reflection_note"):
        parts.append(f"!! ADVICE AFTER GETTING STUCK: {state['reflection_note']}")

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
