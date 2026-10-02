"""Action -> DesktopAdapter calls, in POINTS.

The executor is deliberately dumb. It resolves element ids to coordinates and
dispatches. It does not decide, retry, or interpret — those belong to the graph
(CLAUDE.md §8.5).
"""

import time
from collections.abc import Callable

import structlog

from os_agent.actions.policy import (
    MAX_COMMITS_PER_RUN,
    Verdict,
    check,
    commits_outward,
)
from os_agent.config import settings
from os_agent.desktop.base import DesktopAdapter
from os_agent.env.base import ActionError, ApprovalDenied, PolicyViolation
from os_agent.perception.elements import normalize_role
from os_agent.types import Action, Element, MenuCommand, Receipt

log = structlog.get_logger(__name__)

# Blind wait after each action, so the UI has settled before the next capture.
# Phase B replaces this with poll-until-quiet, which is worth ~1 s/step and is
# one of the larger free wins in §13. Do not tune it before the baseline.
SETTLE_S = 0.5

# A `wait` is the model asking for time. It is not allowed to ask for ten
# minutes: the run would sit idle with a human at the keyboard (§5.3).
MAX_WAIT_MS = 5000

ApproveFn = Callable[[Action, Verdict], bool]


class UncertainDispatch(PolicyViolation):
    """An AX request got no reply. It MAY have run, so it must not be retried —
    "retry when unsure" is correct for a click and catastrophic for a send.
    A PolicyViolation so the run ends rather than the planner trying again."""


class Executor:
    def __init__(
        self,
        adapter: DesktopAdapter,
        approve: ApproveFn | None = None,
        *,
        approval_on: bool = True,
        exec_mode: str | None = None,
    ) -> None:
        self.adapter = adapter
        self.exec_mode = settings.exec_mode if exec_mode is None else exec_mode
        # What the AX path could honestly claim about the last action list.
        # Which MECHANISM ran is part of the result (§4.11), so it is kept.
        self.receipts: list[Receipt] = []
        self.approve = approve
        self.approval_on = approval_on
        # Every action already executed this run. A repeated COMMIT action —
        # Enter in a chat window — is how the same message got sent twice at
        # A5: verification was blind, the agent assumed failure, and retried
        # something that had already worked (§8.6).
        self._executed: set[tuple] = set()
        # Outward commits are counted, not just deduped. Dedup alone does not
        # stop a send loop: the model varied its text slightly each time, so
        # every fingerprint was "new" while every message was unwanted.
        self._commits = 0

    def run(
        self,
        actions: list[Action],
        elements: list[Element],
        *,
        mode: str,
        allowed_bundles: list[str] | None,
        menu: list[MenuCommand] | None = None,
    ) -> None:
        """Execute in order. Any refusal aborts the whole list."""
        self.receipts = []
        menu = menu or []
        for action in actions:
            _, bundle = self.adapter.frontmost_app()
            fingerprint = self._fingerprint(action)
            verdict = check(
                action,
                elements,
                mode=mode,
                frontmost_bundle=bundle,
                allowed_bundles=allowed_bundles,
                approval_on=self.approval_on,
                repeated=fingerprint in self._executed,
                menu=menu,
                exec_mode=self.exec_mode,
            )
            if not verdict.allowed:
                log.warning("policy.refused", kind=action.kind, reason=verdict.reason,
                            recoverable=verdict.recoverable)
                if verdict.recoverable:
                    raise ActionError(verdict.reason)
                raise PolicyViolation(verdict.reason)

            if verdict.needs_approval:
                if self.approve is None or not self.approve(action, verdict):
                    raise ApprovalDenied(f"human declined: {action.kind} ({verdict.reason})")

            if action.is_terminal():
                continue

            if commit := commits_outward(action):
                if self._commits >= MAX_COMMITS_PER_RUN:
                    raise PolicyViolation(
                        f"refusing a {self._commits + 1}th outward commit in one run "
                        f"({commit}). A run that sends this many times is looping, "
                        f"not working — cap is MAX_COMMITS_PER_RUN={MAX_COMMITS_PER_RUN}."
                    )
                self._commits += 1
                log.warning("act.commit", n=self._commits, kind=action.kind, why=commit)

            # THE ADAPTER IS ALLOWED TO BE SURPRISING; the run is not allowed
            # to die of it. press_keys(["cmd"]) raises ValueError ("needs
            # exactly one non-modifier key"), press_keys(["f5"]) raises
            # ValueError ("no macOS keycode"), and pyautogui can raise
            # anything at all. None of those were caught anywhere: the
            # traceback escaped ainvoke, so the run ended with no summary, no
            # trajectory and no record of what it had already done.
            #
            # A malformed action is the planner's mistake to correct, so it
            # arrives as one more `outcome=error` with the reason attached.
            try:
                self._dispatch(action, elements, menu)
            except (ActionError, PolicyViolation, ApprovalDenied):
                # An action that was REFUSED never reached the world, so it
                # must not spend a commit slot. Found by running the newline
                # case against the real adapter: `act.commit n=1` was logged
                # for a `type` that _dispatch then refused, so three bad
                # newlines would have hit the cap and ended the run — a
                # terminal outcome caused entirely by a recoverable mistake.
                if commit:
                    self._commits -= 1
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("act.failed", kind=action.kind,
                            error=f"{type(exc).__name__}: {exc}"[:160])
                raise ActionError(f"{action.kind} failed: {type(exc).__name__}: {exc}") from exc

            self._executed.add(fingerprint)
            time.sleep(SETTLE_S)

    # ----------------------------------------------------------------------
    @staticmethod
    def _fingerprint(action: Action) -> tuple:
        """Identity of an action, for "have I already done this?"."""
        return (action.kind, action.element_id, action.text,
                tuple(action.keys or ()), action.coords)

    def _point(self, action: Action, elements: list[Element]) -> tuple[int, int]:
        if action.coords is not None:
            x, y = action.coords
            w, h = self.adapter.screen_size_points()
            if not (0 <= x < w and 0 <= y < h):
                # An off-screen click is not an error the agent can learn from
                # if we execute it: it simply does nothing and reports
                # no_change, which reads as "that did not work" rather than
                # "you asked for something impossible". Refuse it by name.
                raise ActionError(
                    f"coords ({x},{y}) are outside the {w}x{h} point screen"
                )
            return x, y
        if action.element_id is None:
            raise ActionError(f"{action.kind} needs an element_id or coords")
        for el in elements:
            if el.id == action.element_id:
                return el.center
        # Ids renumber on every observation, so this is the planner using a
        # number it read one screen ago. Ordinary, and entirely recoverable.
        raise ActionError(
            f"element_id {action.element_id} is not on screen "
            f"(ids present: {[e.id for e in elements][:20]})"
        )

    # -- AX path (EXEC_MODE=ax) ----------------------------------------------
    def _element(self, action: Action, elements: list[Element]) -> Element:
        el = next((e for e in elements if e.id == action.element_id), None)
        if el is None:
            raise ActionError(
                f"element_id {action.element_id} is not on screen "
                f"(ids present: {[e.id for e in elements][:20]})"
            )
        return el

    def _guard(self, el: Element) -> None:
        """Act by handle, but only on the node the planner actually saw.

        Handles are positional too — a reordered tree resolves the same path to
        a DIFFERENT node. So re-read it and refuse unless role and name still
        match. The reference repo's example: Calculator's button is "All
        Clear" while the display is clear and "Clear" once it is not.
        """
        node = self.adapter.read_node(el.path)
        if node is None:
            raise ActionError(f"element {el.id} ({el.role} {el.name!r}) is gone")
        role = normalize_role(node.role)
        if role != el.role or node.name != el.name:
            raise ActionError(
                f"element {el.id} changed since it was observed: was "
                f"{el.role} {el.name!r}, now {role} {node.name!r}. Re-read the screen."
            )

    def _settle_receipt(self, r: Receipt) -> None:
        self.receipts.append(r)
        log.info("act.receipt", mechanism=r.mechanism, dispatched=r.dispatched,
                 timed_out=r.timed_out, window_changed=r.window_changed,
                 verified=r.verified, detail=r.detail[:120])
        if r.timed_out:
            raise UncertainDispatch(
                f"{r.mechanism} got no reply and MAY have run ({r.detail}). "
                "Not retrying: a repeat could duplicate the side effect."
            )
        if not r.dispatched:
            raise ActionError(f"{r.mechanism} was not delivered: {r.detail}")
        if r.verified is False:
            raise ActionError(f"{r.mechanism} did not stick: {r.detail}")

    def _dispatch_ax(self, action: Action, elements: list[Element],
                     menu: list[MenuCommand]) -> bool:
        """Handle the action on the AX path. False = not an AX action here,
        fall through to synthetic input."""
        k = action.kind
        if k == "menu":
            cmd = next((m for m in menu if m.id == action.element_id), None)
            if cmd is None:
                raise ActionError(f"menu command m{action.element_id} is not in the menu list")
            node = self.adapter.read_node(cmd.path)
            if node is None or node.name != cmd.title:
                raise ActionError(f"menu command {cmd.title!r} no longer resolves")
            log.info("act.menu", title=cmd.title)
            self._settle_receipt(self.adapter.invoke_menu(cmd.path))
            return True
        if k == "set_value":
            if action.text is None:
                raise ActionError("set_value with no text")
            el = self._element(action, elements)
            if "type" not in el.ops or not el.path:
                raise ActionError(
                    f"element {el.id} ({el.role}) does not accept a written value; "
                    f"its operations are {list(el.ops) or 'unknown'}"
                )
            self._guard(el)
            log.info("act.set_value", element_id=el.id, chars=len(action.text))
            self._settle_receipt(self.adapter.set_value(el.path, action.text))
            return True
        if k == "click" and action.coords is None and action.element_id is not None:
            el = self._element(action, elements)
            if "press" in el.ops and el.path:
                self._guard(el)
                log.info("act.press", element_id=el.id, role=el.role)
                self._settle_receipt(self.adapter.press(el.path))
                return True
            # No press action published: a real click is the only way in.
            # Recorded, because it needs focus where AXPress does not.
            log.info("act.press.fallback", element_id=el.id, why="no AXPress; synthetic click")
            self.receipts.append(Receipt("synthetic_click", dispatched=True,
                                         detail="element publishes no press action"))
        return False

    def _dispatch(self, action: Action, elements: list[Element],
                  menu: list[MenuCommand] | None = None) -> None:
        k = action.kind
        if self.exec_mode == "ax" and self._dispatch_ax(action, elements, menu or []):
            return
        if k in ("set_value", "menu"):
            # policy.check refuses these first; this is the backstop.
            raise ActionError(f"{k} needs EXEC_MODE=ax")
        if k in ("click", "double_click", "right_click"):
            x, y = self._point(action, elements)
            log.info("act.click", kind=k, x=x, y=y, element_id=action.element_id)
            self.adapter.click(x, y, kind=k)
        elif k == "type":
            if action.text is None:
                raise ActionError("type action with no text")
            # ONE ACTION KIND, ONE EFFECT.
            #
            # A newline inside typed text is an invisible Enter. It commits in
            # every message field on the machine, and no label-based guard can
            # see it, because there is no label — it is a character. Refusing
            # it here forces the planner to say what it means with an explicit
            # `key: enter`, which the policy DOES inspect (commits_outward).
            #
            # This is the difference between a guardrail and a guardrail that
            # can be walked around without noticing.
            if "\n" in action.text or "\r" in action.text:
                raise ActionError(
                    "typed text contains a newline, which would send/commit "
                    "silently. Type the text, then emit a separate "
                    "{'kind': 'key', 'keys': ['enter']} action to commit it."
                )
            log.info("act.type", chars=len(action.text))
            self.adapter.type_text(action.text)
        elif k == "key":
            if not action.keys:
                raise ActionError("key action with no keys")
            log.info("act.key", keys=action.keys)
            self.adapter.press_keys(action.keys)
        elif k == "scroll":
            x, y = self._point(action, elements)
            self.adapter.scroll(x, y, action.amount or 3)
        elif k == "wait":
            time.sleep(min(max(action.amount or 1000, 0), MAX_WAIT_MS) / 1000)
        else:
            raise ActionError(f"executor has no handler for action kind {k!r}")
