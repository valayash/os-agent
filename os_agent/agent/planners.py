"""The callables the graph is built from: planner, reflector, verifier.

nodes.py takes these as arguments so the SAME graph runs with stubs (A3) and
with real models (A4+). This file builds the real ones; run.py and the bench
harness only choose which to build and wire them in.

Two planners:
  make_planner          a writing model (any LiteLLM id) returns a PlannedAction
  make_choice_planner   Jev answers named questions instead (CLAUDE.md §19)

Every model call is traced, and every planned step is written to the
trajectory as it happens (§4.6) — never rebuilt from final state afterwards.
"""

import asyncio
import base64
import json
import time

import structlog

from os_agent.agent import choice
from os_agent.agent.nodes import Planner, Reflector, Verifier
from os_agent.agent.prompts import REFLECT_SYSTEM, build_reflect_user, build_user, system_prompt
from os_agent.agent.state import AgentState
from os_agent.config import settings
from os_agent.llm.base import LLMClient, LLMResult
from os_agent.llm.jev_client import JevAnswers
from os_agent.perception.elements import SPARSE_THRESHOLD
from os_agent.telemetry.tracer import CallRecord, Tracer
from os_agent.telemetry.trajectory import StepRecord, TrajectoryWriter, target_of
from os_agent.types import Action, Expectation, Observation, PlannedAction, Reflection, TextValue
from os_agent.verify.cheap import check

log = structlog.get_logger(__name__)


async def _call(client: LLMClient, tracer: Tracer, node: str, step: int,
                **kwargs) -> LLMResult:
    """One traced model call. A failed call is traced too, then re-raised."""
    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(client.plan, **kwargs)
    except Exception as exc:
        tracer.record(CallRecord(
            node=node, step=step, model=client.model, provider=client.provider,
            latency_ms=(time.perf_counter() - t0) * 1000, provider_wait_ms=0.0,
            prompt_tokens=0, completion_tokens=0, cost_usd=0.0, repairs=0,
            attempts=1, ok=False, error=f"{type(exc).__name__}: {exc}"[:200],
        ))
        raise
    tracer.record(CallRecord(
        node=node, step=step, model=result.model, provider=result.provider,
        latency_ms=result.latency_ms, provider_wait_ms=result.provider_wait_ms,
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
        cost_usd=result.cost_usd, repairs=result.repairs,
        attempts=result.raw.get("attempts", 1), ok=True,
    ))
    return result


def _record_step(traj: TrajectoryWriter | None, state: AgentState, result: LLMResult,
                 *, choice_trace: dict | None = None) -> None:
    """One trajectory row, written while the action and its elements are in hand."""
    if traj is None:
        return
    planned: PlannedAction = result.parsed
    els = state.get("elements") or []
    traj.add(StepRecord(
        step=state["step"],
        app=state.get("app", ""), bundle_id=state.get("bundle_id", ""),
        subgoal=planned.subgoal, reasoning=planned.reasoning,
        action=planned.action.__dict__.copy(),
        # §4.7 — the stable key, so this step survives id renumbering
        action_target=target_of(planned.action, els),
        expect=planned.expect.model_dump(),
        # the outcome of the PREVIOUS step; this one has not run yet
        outcome=state.get("last_outcome", ""),
        reason=state.get("last_reason", ""),
        elements=len(els),
        perception_ms=state.get("perception_ms", 0.0),
        llm_latency_ms=result.latency_ms,
        provider_wait_ms=result.provider_wait_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=result.cost_usd, repairs=result.repairs,
        facts=list(state.get("facts") or []),
        reflection_note=state.get("reflection_note", ""),
        choice=choice_trace,
    ))


def make_planner(client: LLMClient, tracer: Tracer, *, screen: tuple[int, int] | None,
                 traj: TrajectoryWriter | None = None) -> Planner:
    """ONE model call per step. Prompt rebuilt from state, never appended (§4.2).

    `screen` is the point size of the display, so the model has a frame for
    `coords` (A5 found it inventing one without it).
    """
    # Chosen once per run: the system prompt stays byte-identical across calls.
    system = system_prompt(exec_mode=settings.exec_mode, menu_actions=settings.menu_actions)

    async def plan(state: AgentState) -> LLMResult:
        result = await _call(
            client, tracer, "plan", state["step"],
            system=system,
            text=build_user(state, screen=screen),
            image_png=base64.b64decode(state.get("screenshot_b64") or ""),
            schema=PlannedAction,
            extra=settings.extra_for(client.model),
        )
        _record_step(traj, state, result)
        return result

    return plan


def make_choice_planner(jev: LLMClient, writer: LLMClient, fallback: Planner,
                        tracer: Tracer, traj: TrajectoryWriter | None = None) -> Planner:
    """The decision as a CHOICE (§19): Jev answers named questions.

    Three routes, decided per step:
      sparse screen   -> `fallback`, the vision planner. Jev takes text only.
      typing step     -> Jev, then ONE `writer` call for the words. Two calls,
                         counted as two (result.raw["calls"]).
      everything else -> Jev alone. One call.
    """

    async def plan(state: AgentState) -> LLMResult:
        els = state.get("elements") or []
        if len(els) < SPARSE_THRESHOLD:
            log.info("choice.fallback", step=state["step"], elements=len(els))
            return await fallback(state)

        req = choice.build_request(state, exec_mode=settings.exec_mode,
                                   menu_actions=settings.menu_actions)
        result = await _call(
            jev, tracer, "plan", state["step"],
            system="", text=json.dumps(req.state, ensure_ascii=False), image_png=b"",
            schema=JevAnswers, extra={"questions": req.questions},
        )
        planned, needs_text, trace = choice.decode(result.parsed.answers, req, elements=els)

        parts = [result]
        if needs_text:
            ctx = {
                "goal": state["goal"],
                "app": state.get("app") or "unknown",
                "operation": planned.action.kind,
                "target": next((choice.label(e) for e in els
                                if e.id == planned.action.element_id), "the focused field"),
                "elements": [choice.label(e) for e in els[:40]],
                "facts": list(state.get("facts") or []),
            }
            text_result = await _call(
                writer, tracer, "text", state["step"],
                system=choice.TEXT_VALUE, text=json.dumps(ctx, ensure_ascii=False),
                image_png=b"", schema=TextValue, extra=settings.extra_for(writer.model),
            )
            planned = choice.with_text(planned, text_result.parsed.text, els)
            parts.append(text_result)

        combined = LLMResult(
            parsed=planned,
            prompt_tokens=sum(p.prompt_tokens for p in parts),
            completion_tokens=sum(p.completion_tokens for p in parts),
            latency_ms=sum(p.latency_ms for p in parts),
            provider_wait_ms=sum(p.provider_wait_ms for p in parts),
            cost_usd=sum(p.cost_usd for p in parts),
            repairs=sum(p.repairs for p in parts),
            model=result.model, provider=result.provider,
            raw={"calls": len(parts), "attempts": result.raw.get("attempts", 1)},
        )
        log.info("choice", step=state["step"], operation=planned.action.kind,
                 confidence=round(planned.confidence, 3), risk=planned.action.risk,
                 calls=len(parts))
        _record_step(traj, state, combined, choice_trace=trace)
        return combined

    return plan


def make_reflector(client: LLMClient, tracer: Tracer) -> Reflector:
    """The extra call, made only when stuck. Its RATE is the metric (§8.8).

    Richer context than the planner, but still no history: more of the CURRENT
    situation, never an accumulating log.
    """

    async def reflect(state: AgentState) -> LLMResult:
        return await _call(
            client, tracer, "reflect", state["step"],
            system=REFLECT_SYSTEM,
            text=build_reflect_user(state),
            image_png=base64.b64decode(state.get("screenshot_b64") or ""),
            schema=Reflection,
            extra=settings.extra_for(client.model),
        )

    return reflect


def make_verifier() -> Verifier:
    """Zero model calls. This is what holds llm_calls/steps at 1.00 (§8.7)."""

    async def verify(state: AgentState, action: Action, expect: Expectation,
                     after: Observation) -> tuple[str, str]:
        return check(
            expect,
            before_elements=state.get("elements") or [],
            after_elements=after.elements,
            before_png=base64.b64decode(state.get("screenshot_plain_b64") or ""),
            after_png=after.screenshot,
            # The expectation is meaningless when the action never ran.
            executor_error=state.get("executor_error") or None,
        )

    return verify
