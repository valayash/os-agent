"""A5 gate (CLAUDE.md §9).

  "Two consecutive failures -> reflect with fuller context. Task and suite
   budgets abort a run. RPM throttle live.
   Gate: the agent recovers from a hostile situation and aborts cleanly when
   the budget is exceeded."

Driven by replay_env so the failures are EXACT. A5 is about control flow under
failure; injecting failure precisely matters more than injecting it realistically,
and macOS has already shown it cannot be relied on to misbehave on cue (§5.3).
"""

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

from os_agent.agent.graph import build_graph, terminal_reason
from os_agent.agent.state import AgentState, initial_state
from os_agent.config import settings
from os_agent.env.replay_env import ReplayEnv, Screen, fake_elements
from os_agent.llm.base import LLMResult
from os_agent.llm.litellm_client import RateLimiter
from os_agent.telemetry.trajectory import StepRecord, TrajectoryWriter, target_of
from os_agent.types import Action, Element, Expectation, PlannedAction, Reflection


def planned(kind: str, **kw) -> PlannedAction:
    return PlannedAction(reasoning="stub", subgoal=f"stub {kind}",
                         action=Action(kind=kind, **kw),
                         expect=Expectation(kind="screen_changed"), confidence=0.5)


def result_of(parsed, cost: float = 0.0) -> LLMResult:
    return LLMResult(parsed=parsed, prompt_tokens=0, completion_tokens=0,
                     latency_ms=0.0, provider_wait_ms=0.0, cost_usd=cost,
                     repairs=0, model="stub", provider="stub")


def verifier_of(outcome: str):
    async def v(state, action, expect, after):
        return outcome, "scripted"
    return v


PASS, FAIL = "PASS", "FAIL"
results: list[bool] = []


def report(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"  {PASS if ok else FAIL}  {name:<34} {detail}")


# --------------------------------------------------------------------------
async def test_reflection_changes_behaviour() -> None:
    """THE test of A5.

    The planner keeps failing and only recovers IF it receives advice. If the
    reflection note never reaches the prompt, this run cannot terminate with
    `done` — it can only hit the step cap. So reaching `done` proves the advice
    actually flowed: reflect -> state -> prompt -> a different decision.
    """
    seen_advice: list[str] = []

    async def planner(state: AgentState):
        if state.get("reflection_note"):
            seen_advice.append(state["reflection_note"])
            return result_of(planned("done"))
        return result_of(planned("click", element_id=0))

    async def reflector(state: AgentState):
        return result_of(Reflection(advice="Stop clicking element 0; it is a label."))

    env = ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 40)])
    app = build_graph(env, planner, verifier_of("error"), reflector, approval=False)
    final = await app.ainvoke(initial_state("recover"), config={"recursion_limit": 300})

    ok = (terminal_reason(final) == "done"
          and bool(seen_advice)
          and final["reflections"] >= 1)
    report("reflection changes behaviour", ok,
           f"steps={final['step']} reflections={final['reflections']} "
           f"advice_seen={len(seen_advice)} -> {terminal_reason(final)}")
    if seen_advice:
        print(f"        advice reached the planner: {seen_advice[0][:60]!r}")


async def test_reflection_is_budgeted() -> None:
    """Advice that does not help must not loop forever."""
    async def planner(state: AgentState):
        return result_of(planned("click", element_id=0))

    async def reflector(state: AgentState):
        return result_of(Reflection(advice="Try harder."))  # deliberately useless

    env = ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 40)])
    app = build_graph(env, planner, verifier_of("error"), reflector, approval=False)
    final = await app.ainvoke(initial_state("stuck"), config={"recursion_limit": 300})
    ok = terminal_reason(final) == "stuck" and final["step"] < settings.max_steps
    report("useless advice still terminates", ok,
           f"steps={final['step']} reflections={final['reflections']} "
           f"-> {terminal_reason(final)}")


async def test_budget_aborts_a_live_run() -> None:
    """Cost accumulates through the plan node and stops the run (§8.10)."""
    per_call = settings.task_budget_usd / 4

    async def planner(state: AgentState):
        return result_of(planned("click", element_id=0), cost=per_call)

    env = ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 40)])
    app = build_graph(env, planner, verifier_of("success"), approval=False)
    final = await app.ainvoke(initial_state("spend"), config={"recursion_limit": 300})
    ok = terminal_reason(final) == "budget" and final["cost_usd"] >= settings.task_budget_usd
    report("budget aborts the run", ok,
           f"steps={final['step']} spent=${final['cost_usd']:.2f} "
           f"of ${settings.task_budget_usd:.2f} -> {terminal_reason(final)}")


def test_rate_limiter() -> None:
    """Pacing beats backoff: avoid the 503 rather than recover from it."""
    r = RateLimiter(rpm=120)  # one every 500 ms
    r.wait()
    t = time.perf_counter()
    paced = r.wait()
    elapsed = (time.perf_counter() - t) * 1000
    ok = 400 < paced < 700 and 400 < elapsed < 700
    report("rpm throttle paces calls", ok, f"waited {paced:.0f} ms at rpm=120")
    report("rpm=0 disables throttling", RateLimiter(0).wait() == 0.0, "no wait")


def test_trajectory_is_replayable() -> None:
    """§4.7: a recorded step must survive element ids renumbering."""
    out = Path("runs/_a5_gate")
    shutil.rmtree(out, ignore_errors=True)
    w = TrajectoryWriter("gate", "goal", "stub", "stub")
    w.dir = out / "gate"
    w.frames = w.dir / "frames"
    w.frames.mkdir(parents=True, exist_ok=True)

    els = [Element(13, "textarea", "alpha Reviewed", (141, 90, 787, 480))]
    action = Action(kind="click", element_id=13)
    w.add(StepRecord(
        step=0, app="TextEdit", bundle_id="com.apple.TextEdit", subgoal="s",
        reasoning="r", action={"kind": "click", "element_id": 13},
        action_target=target_of(action, els), expect={"kind": "screen_changed"},
        outcome="success", reason="ok", elements=1, perception_ms=77.0,
        llm_latency_ms=4365.0, provider_wait_ms=16520.0, prompt_tokens=628,
        completion_tokens=167, cost_usd=0.0011, repairs=0,
    ), screenshot_png=b"\x89PNG\r\n\x1a\nfake")
    path = w.close({"success": True, "steps": 1})

    data = json.loads(path.read_text())
    t = data["steps"][0]["action_target"]
    ok = (t["role"] == "textarea" and t["name"] == "alpha Reviewed"
          and t["bbox"] == [141, 90, 787, 480]
          and data["steps"][0]["screenshot"].endswith(".png")
          and "base64" not in path.read_text())
    report("trajectory is replayable", ok,
           f"target kept as {t['role']}/{t['name']!r}, frame on disk not inlined")
    shutil.rmtree(out, ignore_errors=True)


async def main() -> int:
    print("\nA5 — recovery, budgets, pacing, persistence. No model, no machine.\n")
    print("  " + "-" * 76)
    await test_reflection_changes_behaviour()
    await test_reflection_is_budgeted()
    await test_budget_aborts_a_live_run()
    test_rate_limiter()
    test_trajectory_is_replayable()
    print("  " + "-" * 76)
    print(f"\n  A5 GATE: {sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
