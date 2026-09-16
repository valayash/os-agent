"""LangGraph assembly (CLAUDE.md §8.8).

THIS IS THE ONLY FILE IN THE PROJECT THAT IMPORTS LANGGRAPH.
§13 lists "LangGraph vs custom runtime" as an ablation row — we intend to
measure its overhead and may replace it. A framework that has leaked into
env/, llm/ or verify/ cannot be swapped out, and the ablation becomes
unrunnable. Containing it here is what keeps that experiment possible.

    observe → plan → execute → verify ─┬─ success ──→ observe
                                       ├─ done ─────→ END
                                       ├─ step>=50 ─→ END(fail)
                                       ├─ 2 fails ──→ reflect ─┬→ plan
                                       └─ other ────→ reflect ─┴→ END(fail)
"""

from langgraph.graph import END, StateGraph

from os_agent.agent.nodes import Planner, Verifier, make_nodes
from os_agent.agent.state import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REFLECTIONS,
    AgentState,
)
from os_agent.config import settings
from os_agent.env.base import Environment


def route_after_verify(state: AgentState) -> str:
    """The termination table. Read top to bottom; first match wins.

    ORDER IS THE DESIGN. Terminal conditions are checked BEFORE progress
    conditions, so a run that has both succeeded and exhausted its budget
    stops rather than continuing. Putting `success` first would make the step
    cap advisory, which is how agents run forever.
    """
    action = state.get("last_action")

    # 1. the agent declared itself finished
    if action is not None and action.kind == "done":
        return "done"
    if action is not None and action.kind == "fail":
        return "agent_fail"

    # 2. the guardrails or an exception already ended us
    if state.get("status") != "running":
        return "halted"

    # 3. hard caps — money and time. Checked before any progress condition.
    if state["step"] >= settings.max_steps:
        return "max_steps"
    if state["cost_usd"] >= settings.task_budget_usd:
        return "budget"

    # 4. stuck, in either of its two forms
    if state.get("looping"):
        return "reflect"
    if state["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
        return "reflect"

    # 5. otherwise keep going. A SINGLE failure is not fatal — §11 allows an
    #    action to be retried, just never more than twice.
    return "continue"


def route_after_reflect(state: AgentState) -> str:
    """Reflection is budgeted. Unlimited reflection is an infinite loop with
    extra steps — and each one is a billed model call."""
    if state["reflections"] > MAX_REFLECTIONS:
        return "give_up"
    return "retry"


def build_graph(
    env: Environment,
    planner: Planner,
    verifier: Verifier,
    *,
    checkpointer=None,
    approval: bool | None = None,
):
    """Assemble the graph.

    `planner` and `verifier` are injected so this exact graph runs with stubs
    at A3 and with a real model at A4 — the gate tests the production object.
    """
    nodes = make_nodes(env, planner, verifier)
    g = StateGraph(AgentState)
    for name, fn in nodes.items():
        g.add_node(name, fn)

    g.set_entry_point("observe")
    g.add_edge("observe", "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "verify")

    # Every branch is named and terminates. An unmapped return value from the
    # router raises instead of silently falling through, which is the property
    # that makes "correct termination on every exit path" testable at all.
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "continue": "observe",
            "reflect": "reflect",
            "done": END,
            "agent_fail": END,
            "halted": END,
            "max_steps": END,
            "budget": END,
        },
    )
    g.add_conditional_edges(
        "reflect", route_after_reflect, {"retry": "plan", "give_up": END}
    )

    use_approval = settings.approval if approval is None else approval
    return g.compile(
        checkpointer=checkpointer,
        # Approval is ON by default (§8.6). The pause happens before `execute`
        # — the last moment before the world changes.
        interrupt_before=["execute"] if use_approval else None,
    )


def terminal_reason(state: AgentState) -> str:
    """Why the run ended, for RunResult (§8.10)."""
    if state.get("terminal_reason"):
        return state["terminal_reason"]
    action = state.get("last_action")
    if action is not None and action.kind == "done":
        return "done"
    if action is not None and action.kind == "fail":
        return "agent_fail"
    if state["step"] >= settings.max_steps:
        return "max_steps"
    if state["cost_usd"] >= settings.task_budget_usd:
        return "budget"
    if state["reflections"] > MAX_REFLECTIONS:
        return "stuck"
    return "error"
