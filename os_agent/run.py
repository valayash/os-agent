"""CLI and composition root (CLAUDE.md §8.12).

This is where the real model gets wired into the graph. Everything else in the
project takes its dependencies as arguments; this file is the one place that
decides what those dependencies actually are.

That is why A3's gate and A4's gate exercise the SAME graph: only the two
callables built here differ.
"""

import argparse
import asyncio
import base64
import sys
import time

import structlog

from os_agent.agent.graph import build_graph, terminal_reason
from os_agent.agent.nodes import Planner, Verifier
from os_agent.agent.prompts import SYSTEM, build_user
from os_agent.agent.state import AgentState, initial_state
from os_agent.config import settings
from os_agent.desktop.macos import MacOSAdapter
from os_agent.env.desktop_env import DesktopEnv
from os_agent.llm.litellm_client import LiteLLMClient
from os_agent.telemetry.log import configure
from os_agent.telemetry.tracer import CallRecord, Tracer
from os_agent.types import Action, Expectation, Observation, PlannedAction
from os_agent.verify.cheap import check

log = structlog.get_logger(__name__)


def make_planner(client: LiteLLMClient, tracer: Tracer) -> Planner:
    """The ONE model call per step. Rebuilt prompt, never appended (§4.2)."""

    async def plan(state: AgentState):
        user = build_user(state)
        image = base64.b64decode(state.get("screenshot_b64") or "")
        t0 = time.perf_counter()
        try:
            result = await asyncio.to_thread(
                client.plan,
                system=SYSTEM,
                text=user,
                image_png=image,
                schema=PlannedAction,
                extra=settings.planner_extra(),
            )
        except Exception as exc:
            tracer.record(CallRecord(
                node="plan", step=state["step"], model=client.model,
                provider=client.provider,
                latency_ms=(time.perf_counter() - t0) * 1000,
                provider_wait_ms=0.0, prompt_tokens=0, completion_tokens=0,
                cost_usd=0.0, repairs=0, attempts=1, ok=False,
                error=f"{type(exc).__name__}: {exc}"[:200],
            ))
            raise
        tracer.record(CallRecord(
            node="plan", step=state["step"], model=result.model,
            provider=result.provider, latency_ms=result.latency_ms,
            provider_wait_ms=result.provider_wait_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.cost_usd, repairs=result.repairs,
            attempts=result.raw.get("attempts", 1), ok=True,
        ))
        return result

    return plan


def make_verifier() -> Verifier:
    """Zero model calls. This is what holds llm_calls/steps at 1.00 (§8.7)."""

    async def verify(
        state: AgentState, action: Action, expect: Expectation, after: Observation
    ) -> tuple[str, str]:
        before_png = base64.b64decode(state.get("screenshot_plain_b64") or "")
        return check(
            expect,
            before_elements=state.get("elements") or [],
            after_elements=after.elements,
            before_png=before_png,
            after_png=after.screenshot,
        )

    return verify


def approve_at_terminal(action: Action, verdict) -> bool:
    """Approval prompt. On by default (§8.6); --yolo turns it off."""
    print(f"\n  ABOUT TO: {action.kind}"
          f"{f' element {action.element_id}' if action.element_id is not None else ''}"
          f"{f' {action.text!r}' if action.text else ''}"
          f"{f' {action.keys}' if action.keys else ''}")
    print(f"  reason  : {verdict.reason}")
    try:
        return input("  proceed? [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


async def run_task(goal: str, task_id: str, *, yolo: bool, max_steps: int | None = None):
    configure(pretty=True)
    tracer = Tracer()
    client = LiteLLMClient(settings.planner)
    env = DesktopEnv(
        MacOSAdapter(), mode="bench" if task_id != "freeform" else "freeform",
        approve=None if yolo else approve_at_terminal,
        approval_on=not yolo,
    )
    app = build_graph(env, make_planner(client, tracer), make_verifier(), approval=False)

    state = initial_state(goal=goal, task_id=task_id)
    if task_id != "freeform":
        await env.reset(task_id)
        state["obs_fresh"] = False

    t0 = time.perf_counter()
    final: AgentState = await app.ainvoke(
        state, config={"recursion_limit": (max_steps or settings.max_steps) * 6}
    )
    wall = time.perf_counter() - t0

    score = env.evaluate() if task_id != "freeform" else None
    summary = tracer.summary()
    steps = final["step"] or 1

    print("\n" + "=" * 68)
    print(f"  task            : {task_id}")
    print(f"  terminal reason : {terminal_reason(final)}")
    if score is not None:
        print(f"  checker         : {score}")
    print(f"  steps           : {final['step']}")
    print(f"  llm calls       : {summary['llm_calls']}  "
          f"({summary['llm_calls'] / steps:.2f} per step)")
    print(f"  agent latency   : {summary['agent_latency_s']:.1f} s   <- OURS")
    print(f"  provider wait   : {summary['provider_wait_s']:.1f} s   <- theirs")
    print(f"  wall clock      : {wall:.1f} s")
    print(f"  tokens          : {summary['prompt_tokens']}+{summary['completion_tokens']}")
    print(f"  cost            : ${summary['cost_usd']:.4f}")
    print(f"  schema repairs  : {summary['schema_repair_rate']:.2f}/call")
    print("=" * 68)
    env.close()
    return final, score, summary


def main() -> int:
    ap = argparse.ArgumentParser(prog="os_agent.run")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="a task id from bench/tasks.py")
    g.add_argument("--freeform", help="an arbitrary instruction, no scoring")
    ap.add_argument("--yolo", action="store_true",
                    help="skip approval prompts. NEVER unattended (§11).")
    ap.add_argument("--max-steps", type=int, default=None)
    a = ap.parse_args()

    if a.task:
        from os_agent.bench import tasks
        goal = tasks.get(a.task).goal
        return asyncio.run(run_task(goal, a.task, yolo=a.yolo, max_steps=a.max_steps))[1] != 1.0
    asyncio.run(run_task(a.freeform, "freeform", yolo=a.yolo, max_steps=a.max_steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
