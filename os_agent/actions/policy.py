"""Guardrails. Built at A1, not A5 — before the first automated click.

This agent drives a real machine with real files on it. Everything here exists
because a plausible-looking action can be destructive, and the model has no
concept of which of your files matter (CLAUDE.md §8.6, §11).

Three independent gates, checked in order:
  1. app allowlist   — is the agent even allowed to touch the frontmost app?
  2. action legality — is this action kind permitted in this mode?
  3. irreversibility — does the target look like it destroys or sends something?

Note on "no shell execution, ever": it is enforced by the type system, not by
a check here. ActionKind has no "shell" and no "launch" member, so there is no
value the planner can emit that would run a command. TaskSpec.setup() DOES use
subprocess — that is trusted project code, not the agent's action space.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from os_agent.config import settings
from os_agent.types import Action, Element

# Anything whose target label matches always requires a human (CLAUDE.md §8.6).
IRREVERSIBLE: set[str] = {
    "delete", "remove", "send", "purchase", "buy", "pay", "erase", "format",
    "shut down", "shutdown", "restart", "log out", "sign out",
    "empty trash", "move to trash", "discard", "unsend", "block", "report",
}

# Actions that COMMIT something outward — a message, an email, a form. These
# have no label to match: pressing Enter in a chat window sends a message, and
# IRREVERSIBLE only ever checked the target element's name.
#
# MEASURED A5: a run sent the same WhatsApp message TWICE, because verification
# was blind and the agent retried an action that had already succeeded. The
# guard below exists because "retry when unsure" is correct for a click and
# catastrophic for a send.
COMMIT_KEYS: set[frozenset[str]] = {frozenset({"enter"}), frozenset({"return"})}

# MEASURED 2026-09-17, and it cost a real person eight unwanted messages.
#
# COMMIT_KEYS above only ever looked at action.kind == "key". The agent never
# pressed Enter. It put NEWLINES INSIDE THE TYPED TEXT — 45 characters of
# message plus two "\n" arrived as 47 chars — and type_text typed them
# faithfully. In a chat window a newline IS the send button, so one `type`
# action sent three message fragments ("Bhond", "Subah 10 baje utha dena
# yaarBhond", ...) while the policy saw a harmless keystroke.
#
# Then it got worse, and the reason is the important part. Sending EMPTIES the
# input box. The model looked at the next screenshot, saw an empty box,
# concluded its typing had failed, and typed again. Every retry sent more.
# The verifier called each one `success` because the screen had changed — it
# had: a new message appeared in the thread.
#
# A silent outward commit plus a verifier that reads "something changed" as
# "what I wanted happened" is an unbounded send loop. Both halves are fixed:
# a newline can no longer hide inside `type`, and a repeated commit needs a
# human regardless of --yolo.
MAX_COMMITS_PER_RUN = 3

# Key combinations that destroy things regardless of what the button says.
DANGEROUS_CHORDS: set[frozenset[str]] = {
    frozenset({"command", "delete"}),      # move to trash (Finder)
    frozenset({"command", "shift", "delete"}),  # empty trash
    frozenset({"command", "q"}),           # quit — loses unsaved work
}

# Modes. `bench` is the strict one: a benchmark run must never wander.
MODES = ("bench", "freeform")


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    needs_approval: bool = False
    reason: str = ""

    def __bool__(self) -> bool:  # `if policy.check(...):`
        return self.allowed


def irreversible_hit(label: str) -> str | None:
    """Return the matched term, or None.

    Word-boundary matched on purpose: substring matching flags "undelete" and
    "Deleted Items" as destructive, and a guardrail that cries wolf gets
    switched off.
    """
    low = label.lower()
    for term in IRREVERSIBLE:
        if re.search(rf"\b{re.escape(term)}\b", low):
            return term
    return None


def app_allowed(bundle_id: str, allowed_bundles: list[str] | None) -> bool:
    """None means 'any app' (freeform). A list means exactly that list."""
    if allowed_bundles is None:
        return True
    return bundle_id in allowed_bundles


def path_in_sandbox(path: str | Path) -> bool:
    """Every file the agent touches lives under SANDBOX_DIR (CLAUDE.md §8.6)."""
    try:
        Path(path).expanduser().resolve().relative_to(settings.sandbox.resolve())
        return True
    except (ValueError, OSError):
        return False


def commits_outward(action: Action) -> str | None:
    """Does this action push something into the world? Reason, or None.

    "Outward" means a side effect that leaves the machine or cannot be undone
    from the keyboard: a message sent, a form submitted, an email away. The
    defining property is that the SCREEN AFTERWARDS LOOKS LIKE NOTHING
    HAPPENED — the compose box empties — which is exactly the state that makes
    an agent retry.
    """
    if action.kind == "key" and action.keys:
        if frozenset(k.lower() for k in action.keys) in COMMIT_KEYS:
            return "Enter commits the current input"
    if action.kind == "type" and action.text and ("\n" in action.text or "\r" in action.text):
        return "typed text contains a newline, which commits in any message field"
    return None


def _target_label(action: Action, elements: list[Element]) -> str:
    if action.element_id is None:
        return ""
    for el in elements:
        if el.id == action.element_id:
            return el.name
    return ""


def requires_approval(
    action: Action, elements: list[Element], *, approval_on: bool = True,
    repeated: bool = False,
) -> str | None:
    """Reason a human must confirm, or None.

    `approval_on` is a PARAMETER rather than a global settings read. A4 found
    out why: --yolo set a flag the CLI understood and this function never saw,
    so every action was refused while the logs said approval was "on by
    default". A guardrail that cannot be turned off by the documented switch is
    a bug, not extra safety.

    Note the ordering below — irreversible targets and dangerous chords require
    approval EVEN WITH --yolo. Those are not a convenience prompt; they are the
    line §8.6 draws, and a flag does not move it.
    """
    label = _target_label(action, elements)
    if label and (hit := irreversible_hit(label)):
        return f"target label {label!r} matches irreversible term {hit!r}"

    if action.kind == "key" and action.keys:
        chord = frozenset(k.lower() for k in action.keys)
        if chord in DANGEROUS_CHORDS:
            return f"dangerous key chord {'+'.join(sorted(chord))}"

    # Outward commits, of ANY action kind. `repeated` is the signal that
    # matters: the first send is what the user asked for, the second is the
    # agent failing to notice the first one worked.
    if (commit := commits_outward(action)) and repeated:
        return (f"{commit}, and this exact action already ran this session. "
                "Repeating it duplicates an outward side effect such as "
                "sending a message.")

    if approval_on:
        return "approval is on (default); --yolo disables it"
    return None


def check(
    action: Action,
    elements: list[Element],
    *,
    mode: str,
    frontmost_bundle: str,
    allowed_bundles: list[str] | None,
    approval_on: bool | None = None,
    repeated: bool = False,
) -> Verdict:
    """The single call the executor makes before doing anything."""
    if mode not in MODES:
        return Verdict(False, reason=f"unknown mode {mode!r}; expected one of {MODES}")

    if action.is_terminal():
        return Verdict(True, reason="terminal action, never executed")

    if not app_allowed(frontmost_bundle, allowed_bundles):
        return Verdict(
            False,
            reason=(
                f"frontmost app {frontmost_bundle!r} is not in the task allowlist "
                f"{allowed_bundles!r}. The agent may only act on apps the task declares."
            ),
        )

    if action.kind == "click" and action.element_id is None and action.coords is None:
        return Verdict(False, reason="click with neither element_id nor coords")

    on = settings.approval if approval_on is None else approval_on
    if reason := requires_approval(action, elements, approval_on=on, repeated=repeated):
        return Verdict(True, needs_approval=True, reason=reason)

    return Verdict(True)
