"""Action -> DesktopAdapter calls, in POINTS.

The executor is deliberately dumb. It resolves element ids to coordinates and
dispatches. It does not decide, retry, or interpret — those belong to the graph
(CLAUDE.md §8.5).
"""

import time
from collections.abc import Callable

import structlog

from os_agent.actions.policy import Verdict, check
from os_agent.desktop.base import DesktopAdapter
from os_agent.env.base import ApprovalDenied, PolicyViolation
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
            verdict = check(
                action,
                elements,
                mode=mode,
                frontmost_bundle=bundle,
                allowed_bundles=allowed_bundles,
                approval_on=self.approval_on,
            )
            if not verdict.allowed:
                log.warning("policy.refused", kind=action.kind, reason=verdict.reason)
                raise PolicyViolation(verdict.reason)

            if verdict.needs_approval:
                if self.approve is None or not self.approve(action, verdict):
                    raise ApprovalDenied(f"human declined: {action.kind} ({verdict.reason})")

            if action.is_terminal():
                continue

            self._dispatch(action, elements)
            time.sleep(SETTLE_S)

    # ----------------------------------------------------------------------
    def _point(self, action: Action, elements: list[Element]) -> tuple[int, int]:
        if action.coords is not None:
            return action.coords
        if action.element_id is None:
            raise PolicyViolation(f"{action.kind} needs an element_id or coords")
        for el in elements:
            if el.id == action.element_id:
                return el.center
        raise PolicyViolation(
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
                raise PolicyViolation("type action with no text")
            log.info("act.type", chars=len(action.text))
            self.adapter.type_text(action.text)
        elif k == "key":
            if not action.keys:
                raise PolicyViolation("key action with no keys")
            log.info("act.key", keys=action.keys)
            self.adapter.press_keys(action.keys)
        elif k == "scroll":
            x, y = self._point(action, elements)
            self.adapter.scroll(x, y, action.amount or 3)
        elif k == "wait":
            time.sleep((action.amount or 1000) / 1000)
        else:
            raise PolicyViolation(f"executor has no handler for action kind {k!r}")
