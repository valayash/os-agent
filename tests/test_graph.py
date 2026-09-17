"""Graph routing, checkpointing and the approval pause (CLAUDE.md §8.8)."""

import pytest
from langgraph.checkpoint.memory import MemorySaver

from os_agent.agent.graph import build_graph, route_after_reflect, route_after_verify
from os_agent.agent.state import MAX_REFLECTIONS, initial_state
from os_agent.config import settings
from os_agent.env.replay_env import ReplayEnv, Screen, fake_elements
from os_agent.llm.base import LLMResult
from os_agent.types import Action, Expectation, PlannedAction


def pa(kind="click", **kw):
    return PlannedAction(reasoning="r", subgoal="s", action=Action(kind=kind, **kw),
                         expect=Expectation(kind="screen_changed"), confidence=0.5)


def res(action, cost=0.0):
    return LLMResult(parsed=action, prompt_tokens=0, completion_tokens=0,
                     latency_ms=0.0, provider_wait_ms=0.0, cost_usd=cost,
                     repairs=0, model="stub", provider="stub")


def planner_of(action, cost=0.0):
    async def p(state):
        return res(action, cost)
    return p


def verifier_of(outcome):
    async def v(state, action, expect, after):
        return outcome, "stub"
    return v


def st(**over):
    s = initial_state("g")
    s.update(over)
    return s


# -- the routing table, checked entry by entry against §8.8 -----------------
@pytest.mark.parametrize(
    "state,expected",
    [
        (st(last_action=Action(kind="click")), "continue"),
        (st(last_action=Action(kind="done")), "done"),
        (st(last_action=Action(kind="fail")), "agent_fail"),
        (st(last_action=Action(kind="click"), status="failed"), "halted"),
        (st(last_action=Action(kind="click"), step=settings.max_steps), "max_steps"),
        (st(last_action=Action(kind="click"), cost_usd=1e9), "budget"),
        # ONE failure is retryable; two is not (§11)
        (st(last_action=Action(kind="click"), consecutive_failures=1), "continue"),
        (st(last_action=Action(kind="click"), consecutive_failures=2), "reflect"),
        # the success-loop: nothing failed, and it still must stop
        (st(last_action=Action(kind="click"), looping=True), "reflect"),
    ],
)
def test_routing_table(state, expected):
    assert route_after_verify(state) == expected


def test_terminal_conditions_beat_progress():
    """A run that finished AND blew its budget reports `done`, not `budget` —
    the work actually completed. But the cap must still be checked before any
    progress condition, or the step limit becomes advisory."""
    assert route_after_verify(st(last_action=Action(kind="done"), cost_usd=1e9)) == "done"
    assert route_after_verify(
        st(last_action=Action(kind="click"), step=999, consecutive_failures=0)
    ) == "max_steps"


def test_reflection_is_budgeted():
    assert route_after_reflect(st(reflections=1)) == "retry"
    assert route_after_reflect(st(reflections=MAX_REFLECTIONS + 1)) == "give_up"


# -- the approval pause -----------------------------------------------------
async def test_interrupt_before_execute_pauses_the_world():
    """Approval is on by default (§8.6). The pause must land BEFORE execute —
    the last moment before anything changes on the real machine."""
    env = ReplayEnv([Screen(elements=fake_elements(3))])
    app = build_graph(env, planner_of(pa()), verifier_of("success"),
                      checkpointer=MemorySaver(), approval=True)
    cfg = {"configurable": {"thread_id": "t1"}}
    await app.ainvoke(initial_state("g"), config=cfg)

    snap = await app.aget_state(cfg)
    assert snap.next == ("execute",), f"paused at {snap.next}, expected execute"
    assert env.acted == [], "the environment was touched despite the interrupt"
    # a plan was made, it just has not been carried out
    assert snap.values["last_action"] is not None


async def test_resume_from_the_pause_executes():
    env = ReplayEnv([Screen(elements=fake_elements(3))])
    app = build_graph(env, planner_of(pa(element_id=0)), verifier_of("success"),
                      checkpointer=MemorySaver(), approval=True)
    cfg = {"configurable": {"thread_id": "t2"}}
    await app.ainvoke(initial_state("g"), config=cfg)
    assert env.acted == []
    await app.ainvoke(None, config=cfg)  # approve: resume from the checkpoint
    assert len(env.acted) >= 1, "resuming did not execute the pending action"


# -- checkpointing ----------------------------------------------------------
async def test_state_survives_between_invocations():
    """A 40-step run that crashes should resume, not restart (§8.8)."""
    env = ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 20)])
    app = build_graph(env, planner_of(pa(element_id=0)), verifier_of("success"),
                      checkpointer=MemorySaver(), approval=True)
    cfg = {"configurable": {"thread_id": "t3"}}
    await app.ainvoke(initial_state("g", "task-x"), config=cfg)
    first = (await app.aget_state(cfg)).values
    await app.ainvoke(None, config=cfg)
    second = (await app.aget_state(cfg)).values
    assert second["goal"] == first["goal"] == "g"
    assert second["task_id"] == "task-x"
    assert second["llm_calls"] >= first["llm_calls"]


# -- the property the whole design rests on ---------------------------------
async def test_state_does_not_grow_with_steps():
    """§4.2: no accumulating fields. If this fails, the project's thesis is
    gone — the prompt would grow and late steps would cost 3x early ones."""
    # 57 DISTINCT screens. The first version of this test reused one screen
    # object, which is an oscillation — the loop detector stopped it at step 12,
    # correctly, and the test blamed the graph. Fixtures have to be as careful
    # as the code they exercise.
    env = ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 60)])
    app = build_graph(env, planner_of(pa(element_id=0)), verifier_of("success"),
                      approval=False)
    final = await app.ainvoke(initial_state("g"), config={"recursion_limit": 300})

    assert final["step"] == settings.max_steps
    assert len(final["facts"]) <= 10
    assert len(final["recent_hashes"]) <= 8
    for forbidden in ("messages", "history", "screenshots", "observations"):
        assert forbidden not in final, f"{forbidden!r} appeared in state — §11 violation"


def test_langgraph_is_imported_in_exactly_one_file():
    """CLAUDE.md A3 claims this and nothing enforced it.

    §13 lists "LangGraph vs custom runtime" as an ablation row. A framework
    that has leaked into env/, llm/, run.py or verify/ cannot be swapped, and
    the row becomes unrunnable. run.py needed the recursion error and nearly
    imported langgraph directly to get it; graph.py re-exports it instead.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "os_agent"
    leaked = [
        str(f.relative_to(root))
        for f in root.rglob("*.py")
        if "langgraph" in f.read_text() and f.name != "graph.py"
    ]
    assert not leaked, f"langgraph leaked outside agent/graph.py: {leaked}"
