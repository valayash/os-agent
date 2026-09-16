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

import time
from collections.abc import Awaitable, Callable

import structlog

from os_agent.agent.state import (
    AgentState,
    is_looping,
    push_fact,
    push_hash,
)
from os_agent.env.base import ApprovalDenied, Environment, PolicyViolation
from os_agent.llm.base import LLMResult
from os_agent.types import Action, Expectation, PlannedAction

log = structlog.get_logger(__name__)

# planner: state -> an LLMResult whose .parsed is a PlannedAction.
#
# It returns the RESULT, not just the action, because cost and token counts
# have to reach state and only a NODE can put them there — a callable cannot
# mutate state (LangGraph merges what a node RETURNS and discards the rest).
# Learned by writing a budget test that silently did nothing.
Planner = Callable[[AgentState], Awaitable[LLMResult]]
# verifier: (state, action, expect) -> outcome. NO LLM unless ambiguous (§8.7)
Verifier = Callable[[AgentState, Action, Expectation], Awaitable[str]]


def make_nodes(env: Environment, planner: Planner, verifier: Verifier) -> dict:
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
            "screenshot_b64": "",  # A4 fills this; stubs do not need bytes
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
            return {}
        except (PolicyViolation, ApprovalDenied) as exc:
            # A refusal is not a failed attempt — it is the guardrails working.
            # It ends the run rather than being retried (§8.6).
            log.warning("execute.refused", step=state["step"], error=str(exc)[:120])
            return {"status": "failed", "terminal_reason": "policy",
                    "last_outcome": "error"}

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

        outcome = await verifier(state, action, expect)
        obs = await env.observe()

        # Loop detection is over (screen, action), not screen alone: returning
        # to a screen is normal; returning to it and doing the SAME thing is a
        # loop (§8.8).
        fingerprint = f"{state.get('screen_hash', '')}|{action.kind}|{action.element_id}"
        looping = is_looping(state["recent_hashes"], fingerprint)

        failures = 0 if outcome == "success" else state["consecutive_failures"] + 1
        log.info("verify", step=state["step"], outcome=outcome,
                 consecutive_failures=failures, looping=looping)

        return {
            "last_outcome": outcome,
            "consecutive_failures": failures,
            "recent_hashes": push_hash(state["recent_hashes"], fingerprint),
            "looping": looping,
            "step": state["step"] + 1,
            # the capture we just took IS the next step's observation
            "app": obs.meta.get("app", ""),
            "bundle_id": obs.meta.get("bundle_id", ""),
            "elements": obs.elements,
            "screen_hash": obs.meta.get("screen_hash", ""),
            "obs_fresh": True,
        }

    async def reflect(state: AgentState) -> AgentState:
        """The escape hatch, not a step.

        Every time this fires, llm_calls/steps rises above 1.0 — so its RATE is
        a metric, not just its existence. A5 gives it real content; here it
        only counts and clears the failure streak so plan gets a fresh attempt.
        """
        log.warning("reflect", step=state["step"], reflections=state["reflections"] + 1,
                    last_outcome=state.get("last_outcome"))
        return {
            "reflections": state["reflections"] + 1,
            "llm_calls": state["llm_calls"] + 1,  # it IS an extra call. Count it.
            "consecutive_failures": 0,
            "recent_hashes": [],  # give the retry a clean slate
            "looping": False,
        }

    return {"observe": observe, "plan": plan, "execute": execute,
            "verify": verify, "reflect": reflect}
