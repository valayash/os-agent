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
from os_agent.desktop.base import DesktopAdapter
from os_agent.env.base import ActionError, ApprovalDenied, PolicyViolation
from os_agent.types import Action, Element

log = structlog.get_logger(__name__)

# Blind wait after each action, so the UI has settled before the next capture.
# Phase B replaces this with poll-until-quiet, which is worth ~1 s/step and is
# one of the larger free wins in §13. Do not tune it before the baseline.
SETTLE_S = 0.5

ApproveFn = Callable[[Action, Verdict], bool]


class Executor:
    def __init__(
        self,
        adapter: DesktopAdapter,
        approve: ApproveFn | None = None,
        *,
        approval_on: bool = True,
    ) -> None:
        self.adapter = adapter
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
    ) -> None:
        """Execute in order. Any refusal aborts the whole list."""
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
                self._dispatch(action, elements)
            except (ActionError, PolicyViolation, ApprovalDenied):
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

    def _dispatch(self, action: Action, elements: list[Element]) -> None:
        k = action.kind
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
            time.sleep((action.amount or 1000) / 1000)
        else:
            raise ActionError(f"executor has no handler for action kind {k!r}")
