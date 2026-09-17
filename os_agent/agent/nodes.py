"""The five nodes (CLAUDE.md §8.8).

NODE CONTRACT — each node returns ONLY the state keys it changed, and never
mutates state in place. LangGraph merges the returned dict over the current
state; a node that mutates has already applied its change before the
checkpointer sees it, and every replayed run then lies about what happened.

DEPENDENCY INJECTION — nodes take `planner` and `verifier` as arguments rather
than importing llm/ and verify/ themselves. At A3 those are stubs; at A4 they
become a real model call and a real expectation check. The graph is IDENTICAL
in both cases, which means A3's gate tests the production graph rather than a
parallel mock of it.
"""

import base64
import time
from collections.abc import Awaitable, Callable

import structlog

from os_agent.agent.state import (
    REFLECTION_CAP,
    AgentState,
    is_looping,
    push_fact,
    push_hash,
)
from os_agent.env.base import (
    ActionError,
    ApprovalDenied,
    Environment,
    PolicyViolation,
)
from os_agent.llm.base import LLMResult
from os_agent.types import Action, Expectation, Observation, PlannedAction

log = structlog.get_logger(__name__)

# planner: state -> an LLMResult whose .parsed is a PlannedAction.
#
# It returns the RESULT, not just the action, because cost and token counts
# have to reach state and only a NODE can put them there — a callable cannot
# mutate state (LangGraph merges what a node RETURNS and discards the rest).
# Learned by writing a budget test that silently did nothing.
Planner = Callable[[AgentState], Awaitable[LLMResult]]
# verifier: (state, action, expect, after) -> (outcome, reason). NO LLM (§8.7).
#
# It takes the AFTER observation because verification is a comparison: what the
# screen looked like before (carried in state) against what it looks like now.
# The first version of this called the verifier BEFORE taking the post-action
# observation, which meant it could only ever see the stale screen.
Verifier = Callable[
    [AgentState, Action, Expectation, "Observation"], Awaitable[tuple[str, str]]
]
# reflector: state -> LLMResult whose .parsed is a Reflection. OPTIONAL — when
# absent, reflect degrades to counting, which is what A3's stub graph wants.
Reflector = Callable[[AgentState], Awaitable["LLMResult"]] | None


def make_nodes(
    env: Environment,
    planner: Planner,
    verifier: Verifier,
    reflector: Reflector = None,
) -> dict:
    async def observe(state: AgentState) -> AgentState:
        """Capture the screen — unless verify already did (§8.8).

        ONE CAPTURE PER STEP. verify has to look at the post-action screen to
        decide the outcome, and that IS the next step's observation. Capturing
        again here would burn 150-300 ms per step for an identical picture.
        """
        if state.get("obs_fresh"):
            log.debug("observe.skip", step=state["step"], reason="verify already captured")
            return {"obs_fresh": False}

        t0 = time.perf_counter()
        obs = await env.observe()
        return {
            "app": obs.meta.get("app", ""),
            "bundle_id": obs.meta.get("bundle_id", ""),
            "elements": obs.elements,
            # The ANNOTATED image — the one with the numbered boxes. That is
            # what the planner looks at; the clean screenshot is only evidence
            # for the pixel diff in verify.
            "screenshot_b64": base64.b64encode(obs.annotated).decode(),
            "screenshot_plain_b64": base64.b64encode(obs.screenshot).decode(),
            "screen_hash": obs.meta.get("screen_hash", ""),
            "obs_fresh": False,
            "perception_ms": (time.perf_counter() - t0) * 1000,
        }

    async def plan(state: AgentState) -> AgentState:
        """THE model call. The only one in a healthy step (§1).

        This node owns cost accounting. The budget can only be enforced from
        here, and only to within ONE call — you cannot know what a call costs
        before making it (§8.10).
        """
        result: LLMResult = await planner(state)
        planned: PlannedAction = result.parsed
        log.info(
            "plan",
            step=state["step"],
            kind=planned.action.kind,
            subgoal=planned.subgoal[:40],
            confidence=planned.confidence,
            cost_usd=round(result.cost_usd, 5),
        )
        return {
            "subgoal": planned.subgoal,
            "reasoning": planned.reasoning,
            "last_action": planned.action,
            "last_expect": planned.expect,
            "llm_calls": state["llm_calls"] + 1,
            "cost_usd": state["cost_usd"] + result.cost_usd,
            "facts": push_fact(state["facts"], planned.new_fact),
        }

    async def execute(state: AgentState) -> AgentState:
        """Act on the world. Terminal actions never reach the environment."""
        action = state["last_action"]
        if action is None or action.is_terminal():
            return {}
        try:
            await env.act([action])  # a LIST, always (§4.1)
            return {"executor_error": ""}
        except (PolicyViolation, ApprovalDenied) as exc:
            # A refusal is not a failed attempt — it is the guardrails working.
            # It ends the run rather than being retried (§8.6).
            log.warning("execute.refused", step=state["step"], error=str(exc)[:120])
            return {"status": "failed", "terminal_reason": "policy",
                    "last_outcome": "error"}
        except ActionError as exc:
            # The OTHER kind of failure: this attempt could not run, but the
            # planner can choose differently next step. It must NOT end the
            # run — a stale element_id used to do exactly that, and ids
            # renumber on every observation.
            #
            # It is handed to verify rather than returned as an outcome here,
            # so it flows through the one place that decides outcomes and
            # counts toward consecutive_failures like any other failure.
            log.warning("execute.action_error", step=state["step"],
                        error=str(exc)[:160])
            return {"executor_error": str(exc)[:200]}

    async def verify(state: AgentState) -> AgentState:
        """Check the expectation the planner already emitted. NO model call.

        Also performs the post-action capture and hands it forward as the next
        step's observation, which is what `obs_fresh` signals.
        """
        action, expect = state["last_action"], state["last_expect"]
        if state.get("status") != "running":  # execute already terminated us
            return {}
        if action is None or action.is_terminal():
            return {"step": state["step"] + 1}

        # Observe FIRST — this capture is both the evidence for verification
        # and the next step's observation. One capture per step (§8.8).
        obs = await env.observe()
        outcome, reason = await verifier(state, action, expect, obs)

        # Loop detection is over (screen, action), not screen alone: returning
        # to a screen is normal; returning to it and doing the SAME thing is a
        # loop (§8.8).
        fingerprint = f"{state.get('screen_hash', '')}|{action.kind}|{action.element_id}"
        looping = is_looping(state["recent_hashes"], fingerprint)

        failures = 0 if outcome == "success" else state["consecutive_failures"] + 1
        log.info("verify", step=state["step"], outcome=outcome, reason=reason,
                 consecutive_failures=failures, looping=looping)

        return {
            "last_outcome": outcome,
            "last_reason": reason,
            "consecutive_failures": failures,
            "recent_hashes": push_hash(state["recent_hashes"], fingerprint),
            "looping": looping,
            "step": state["step"] + 1,
            # the capture we just took IS the next step's observation
            "app": obs.meta.get("app", ""),
            "bundle_id": obs.meta.get("bundle_id", ""),
            "elements": obs.elements,
            "screenshot_b64": base64.b64encode(obs.annotated).decode(),
            "screenshot_plain_b64": base64.b64encode(obs.screenshot).decode(),
            "screen_hash": obs.meta.get("screen_hash", ""),
            "obs_fresh": True,
            "executor_error": "",  # consumed; never carried into the next step
        }

    async def reflect(state: AgentState) -> AgentState:
        """The escape hatch, not a step.

        Every time this fires, llm_calls/steps rises above 1.0 — so its RATE is
        a metric, not just its existence. Making it USEFUL is the point; making
        it RARE is the design.

        It produces one short strategy note, which overwrites the previous one.
        Not a history: a single slot, capped, so the prompt still never grows.
        """
        note = ""
        cost = 0.0
        if reflector is not None:
            try:
                result = await reflector(state)
                note = str(result.parsed.advice)[:REFLECTION_CAP]
                cost = result.cost_usd
            except Exception as exc:  # noqa: BLE001
                # A failed reflection must not kill a run that still has steps
                # left. Degrade to counting and let the planner try again.
                log.warning("reflect.failed", error=f"{type(exc).__name__}: {exc}"[:160])

        log.warning("reflect", step=state["step"], reflections=state["reflections"] + 1,
                    last_outcome=state.get("last_outcome"), advice=note[:80])
        return {
            "reflections": state["reflections"] + 1,
            "llm_calls": state["llm_calls"] + 1,  # it IS an extra call. Count it.
            "cost_usd": state["cost_usd"] + cost,
            "reflection_note": note or state.get("reflection_note", ""),
            "consecutive_failures": 0,
            "recent_hashes": [],  # give the retry a clean slate
            "looping": False,
        }

    return {"observe": observe, "plan": plan, "execute": execute,
            "verify": verify, "reflect": reflect}
