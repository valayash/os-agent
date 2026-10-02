"""CLI and composition root (CLAUDE.md §8.12).

The one place that decides what the graph's dependencies actually are: which
model, which planner, which adapter. Everything it wires together lives
elsewhere — planners in agent/planners.py, the loop in agent/graph.py.
"""

import argparse
import asyncio
import os
import sys
import time

import structlog

from os_agent.agent.graph import build_graph, run_graph, terminal_reason
from os_agent.agent.planners import (
    make_choice_planner,
    make_planner,
    make_reflector,
    make_verifier,
)
from os_agent.agent.state import AgentState, initial_state
from os_agent.config import settings
from os_agent.env.desktop_env import DesktopEnv
from os_agent.llm.jev_client import JevClient
from os_agent.llm.litellm_client import LiteLLMClient
from os_agent.telemetry.log import configure
from os_agent.telemetry.tracer import Tracer
from os_agent.telemetry.trajectory import TrajectoryWriter
from os_agent.types import Action

log = structlog.get_logger(__name__)


def approve_at_terminal(action: Action, verdict) -> bool:
    """Approval prompt. On by default (§8.6); --yolo turns routine ones off."""
    print(f"\n  ABOUT TO: {action.kind}"
          f"{f' element {action.element_id}' if action.element_id is not None else ''}"
          f"{f' {action.text!r}' if action.text else ''}"
          f"{f' {action.keys}' if action.keys else ''}")
    print(f"  reason  : {verdict.reason}")
    try:
        return input("  proceed? [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


def preflight(adapter, app_name: str) -> None:
    """Bring `app_name` forward and fit its window, or abort.

    Switching apps is not in the agent's action space and cannot be: macOS
    suppresses programmatic focus changes for a background process, except
    shortly after real human input — which is now, because a person just
    pressed Enter on this command (§5.3). Verified, never assumed: a run aimed
    at the wrong app produces a plausible trajectory of meaningless steps.

    Fitting the window (maximise, NEVER fullscreen) cut a capture from 2,393 KB
    to 86 KB at A5 (§13) and makes every run see the same layout.
    """
    if not adapter.activate_app(app_name):
        front, bundle = adapter.frontmost_app()
        log.error("preflight.focus_failed", want=app_name, got=front, bundle=bundle)
        print(f"\n  ABORT: could not bring {app_name!r} to the front (frontmost is "
              f"{front!r}).\n  Click the app once yourself, then re-run. §5.3.\n")
        raise SystemExit(2)
    adapter.fit_frontmost_window()
    time.sleep(0.4)  # let the resize settle before the first capture
    log.info("preflight.ok", app=adapter.frontmost_app()[0])


async def run_task(goal: str, task_id: str, *, yolo: bool, max_steps: int | None = None,
                   app_name: str | None = None) -> tuple[AgentState, float | None, dict]:
    configure(pretty=True)
    # Imported here so the rest of this module imports on a machine without pyobjc.
    from os_agent.desktop.macos import MacOSAdapter

    # A choice backend (Jev) cannot write: typed text, reflection and the
    # vision fallback for sparse screens go to MODEL_SMALL instead (§19).
    if settings.planner_is_choice:
        if not os.getenv("JEV_API_KEY"):
            raise SystemExit("MODEL_PLANNER is a Jev model but JEV_API_KEY is not set.")
        client = JevClient(settings.planner)
        writer = LiteLLMClient(settings.small)
    else:
        client = writer = LiteLLMClient(settings.planner)

    tracer = Tracer()
    # Which ablation row this run belongs to; a run without these is unlabelled.
    traj = TrajectoryWriter(task_id, goal, client.model, client.provider, extra={
        "exec_mode": settings.exec_mode, "menu_actions": settings.menu_actions,
        "ax_max_depth": settings.ax_max_depth, "risk_approval": settings.risk_approval,
    })
    adapter = MacOSAdapter()
    if app_name:
        preflight(adapter, app_name)

    env = DesktopEnv(
        adapter, mode="bench" if task_id != "freeform" else "freeform",
        # MEASURED A5: a terminal prompt makes the TERMINAL frontmost, so the
        # approved action then fires at it instead of the target app. The
        # callback is still passed with approval off, because irreversible
        # targets and dangerous chords ask regardless (§8.6).
        approve=approve_at_terminal,
        approval_on=settings.approval and not yolo,
    )
    planner = make_planner(writer, tracer, screen=adapter.screen_size_points(), traj=traj)
    if settings.planner_is_choice:
        planner = make_choice_planner(client, writer, planner, tracer, traj)
    app = build_graph(env, planner, make_verifier(), make_reflector(writer, tracer),
                      approval=False)

    state = initial_state(goal=goal, task_id=task_id, max_steps=max_steps)
    if task_id != "freeform":
        await env.reset(task_id)

    cap = max_steps or settings.max_steps
    t0 = time.perf_counter()
    final = await run_graph(app, state, recursion_limit=cap * 6)
    wall = time.perf_counter() - t0

    score = env.evaluate() if task_id != "freeform" else None
    summary = tracer.summary()
    reason = terminal_reason(final)
    detail = f"  ({final.get('last_reason', '')})" if reason == "error" else ""
    steps = final["step"] or 1

    print("\n" + "=" * 68)
    print(f"  task            : {task_id}")
    print(f"  exec mode       : {settings.exec_mode}{'  +menu' if settings.menu_actions else ''}")
    print(f"  terminal reason : {reason}{detail}")
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
    print(f"  reflections     : {final['reflections']}")

    path = traj.close({
        "success": score == 1.0 if score is not None else None,
        "terminal_reason": reason,
        "steps": final["step"], "wall_clock_s": round(wall, 2), **summary,
    })
    print(f"  trajectory      : {path}")
    print("=" * 68)
    env.close()
    return final, score, summary


def main() -> int:
    ap = argparse.ArgumentParser(prog="os_agent.run")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--task", help="a task id from bench/tasks.py")
    g.add_argument("--freeform", help="an arbitrary instruction, no scoring")
    ap.add_argument("--yolo", action="store_true",
                    help="skip routine approval prompts. NEVER unattended (§11).")
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--app", default=None,
                    help="app to focus and fit to screen BEFORE the run (§5.3)")
    a = ap.parse_args()

    if a.task:
        from os_agent.bench import tasks
        goal = tasks.get(a.task).goal
        _, score, _ = asyncio.run(run_task(goal, a.task, yolo=a.yolo,
                                           max_steps=a.max_steps, app_name=a.app))
        return 0 if score == 1.0 else 1
    asyncio.run(run_task(a.freeform, "freeform", yolo=a.yolo, max_steps=a.max_steps,
                         app_name=a.app))
    return 0


if __name__ == "__main__":
    sys.exit(main())
