"""A3 gate (CLAUDE.md §9).

  "LangGraph with stub nodes that only print, driven by replay_env. Verify
   routing, cycles, termination, step cap, loop detection — before any model
   call and without touching the real machine.
   Gate: 50 stub steps, correct termination on every exit path."

No model, no screen, no permissions. Every scenario below drives the SAME
graph that A4 will use — only `planner` and `verifier` differ.
"""

import asyncio
import sys

from os_agent.agent.graph import build_graph, terminal_reason
from os_agent.agent.state import AgentState, initial_state
from os_agent.config import settings
from os_agent.env.base import PolicyViolation
from os_agent.env.replay_env import ReplayEnv, Screen, fake_elements
from os_agent.llm.base import LLMResult
from os_agent.types import Action, Expectation, PlannedAction


def stub_result(action: PlannedAction, cost: float = 0.0) -> LLMResult:
    """What a real LLMClient.plan() returns, with zeros for the telemetry."""
    return LLMResult(parsed=action, prompt_tokens=0, completion_tokens=0,
                     latency_ms=0.0, provider_wait_ms=0.0, cost_usd=cost,
                     repairs=0, model="stub", provider="stub")


def planned(kind: str, **kw) -> PlannedAction:
    return PlannedAction(
        reasoning="stub", subgoal=f"stub {kind}",
        action=Action(kind=kind, **kw),
        expect=Expectation(kind="screen_changed"),
        confidence=0.5,
    )


def scripted_planner(script: list[PlannedAction], repeat_last: bool = True,
                     cost: float = 0.0):
    async def plan(state: AgentState) -> LLMResult:
        i = state["step"]
        pa = script[i] if i < len(script) else (
            script[-1] if repeat_last else planned("done"))
        return stub_result(pa, cost)
    return plan


def fixed_verifier(outcome: str):
    async def verify(state, action, expect) -> str:
        return outcome
    return verify


async def run(name: str, *, env, planner, verifier, expect_reason: str,
              expect_steps=None, max_iter: int = 400) -> bool:
    app = build_graph(env, planner, verifier, approval=False)
    state = initial_state(goal=f"stub: {name}")
    final: AgentState = await app.ainvoke(state, config={"recursion_limit": max_iter})
    reason = terminal_reason(final)
    ok = reason == expect_reason
    if expect_steps is not None:
        ok = ok and final["step"] == expect_steps
    ratio = final["llm_calls"] / final["step"] if final["step"] else 0
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<30} "
          f"steps={final['step']:>3} calls={final['llm_calls']:>3} "
          f"calls/step={ratio:.2f} reflect={final['reflections']} -> {reason}")
    if not ok:
        print(f"        expected {expect_reason}"
              + (f" at step {expect_steps}" if expect_steps else ""))
    return ok


async def main() -> int:
    print("\nA3 — routing, cycles, termination. No model, no machine.\n")
    print(f"  {'':<6}{'scenario':<30} {'result'}")
    print("  " + "-" * 78)
    results = []

    # 1. the happy path: three steps of work, then the model says done
    results.append(await run(
        "done after 3 steps",
        env=ReplayEnv([Screen(elements=fake_elements(n)) for n in (3, 4, 5, 6)]),
        planner=scripted_planner([planned("click", element_id=0)] * 3 + [planned("done")],
                                 repeat_last=False),
        verifier=fixed_verifier("success"),
        expect_reason="done", expect_steps=4,
    ))

    # 2. the step cap. Screens keep changing and every action succeeds, so
    #    NOTHING except the cap can stop this. That is the point.
    results.append(await run(
        "step cap holds at 50",
        env=ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 60)]),
        planner=scripted_planner([planned("click", element_id=0)]),
        verifier=fixed_verifier("success"),
        expect_reason="max_steps", expect_steps=settings.max_steps,
    ))

    # 3. the agent gives up on itself
    results.append(await run(
        "agent emits fail",
        env=ReplayEnv([Screen(elements=fake_elements(n)) for n in (3, 4)]),
        planner=scripted_planner([planned("click", element_id=0), planned("fail")],
                                 repeat_last=False),
        verifier=fixed_verifier("success"),
        expect_reason="agent_fail",
    ))

    # 4. everything fails -> 2 strikes -> reflect -> retry -> ... -> give up
    results.append(await run(
        "repeated failure -> stuck",
        env=ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 40)]),
        planner=scripted_planner([planned("click", element_id=0)]),
        verifier=fixed_verifier("error"),
        expect_reason="stuck",
    ))

    # 5. THE NASTY ONE: two screens, same action, EVERY step reports success.
    #    consecutive_failures never rises. Only the loop detector can stop it.
    results.append(await run(
        "oscillation (all 'success')",
        env=ReplayEnv([Screen(app="A", elements=fake_elements(3)),
                       Screen(app="B", elements=fake_elements(4))]),
        planner=scripted_planner([planned("click", element_id=0)]),
        verifier=fixed_verifier("success"),
        expect_reason="stuck",
    ))

    # 6. guardrails refuse -> the run ends, it is NOT retried
    class RefusingEnv(ReplayEnv):
        async def act(self, actions):
            raise PolicyViolation("frontmost app not in the task allowlist")

    results.append(await run(
        "policy refusal ends the run",
        env=RefusingEnv([Screen()]),
        planner=scripted_planner([planned("click", element_id=0)]),
        verifier=fixed_verifier("success"),
        expect_reason="policy", expect_steps=0,
    ))

    # 7. budget
    async def spending_verifier(state, action, expect):
        return "success"

    class SpendingEnv(ReplayEnv):
        async def observe(self):
            return await super().observe()

    env7 = SpendingEnv([Screen(elements=fake_elements(n)) for n in range(3, 40)])

    # Each call costs a third of the task budget, so the 3rd step trips it.
    # Cost reaches state because the PLAN NODE adds it — a planner cannot
    # mutate state, which is what the first version of this test got wrong.
    results.append(await run(
        "budget abort",
        env=env7,
        planner=scripted_planner([planned("click", element_id=0)],
                                 cost=settings.task_budget_usd / 3),
        verifier=spending_verifier,
        expect_reason="budget", expect_steps=3,
    ))

    print("  " + "-" * 78)
    passed = sum(results)
    print(f"\n  A3 GATE: {passed}/{len(results)} exit paths correct")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
