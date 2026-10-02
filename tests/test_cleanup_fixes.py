"""Regression tests for the bugs found in the code review of 2026-10-02.

Each test names the bug it pins. All run without a Mac.
"""

import asyncio

import pytest

from os_agent.actions import executor as executor_mod
from os_agent.actions import policy
from os_agent.actions.executor import MAX_WAIT_MS, Executor
from os_agent.agent.graph import build_graph, run_graph, terminal_reason
from os_agent.agent.state import initial_state
from os_agent.env.replay_env import ReplayEnv, Screen, fake_elements
from os_agent.llm.base import LLMResult
from os_agent.types import Action, Element, Expectation, PlannedAction
from os_agent.verify.cheap import check, error_dialog


def _res(kind="click", **kw):
    p = PlannedAction(reasoning="r", subgoal="s", action=Action(kind=kind, **kw),
                      expect=Expectation(kind="screen_changed"), confidence=0.5)
    return LLMResult(parsed=p, prompt_tokens=0, completion_tokens=0, latency_ms=0.0,
                     provider_wait_ms=0.0, cost_usd=0.0, repairs=0, model="m", provider="p")


async def _ok(state, action, expect, after):
    return "success", "ok"


def _env():
    # Screens never repeat, so the loop detector does not end these runs.
    return ReplayEnv([Screen(elements=fake_elements(n)) for n in range(3, 40)])


# -- run_graph: an abnormal end keeps the real state ----------------------------
def test_planner_error_ends_the_run_with_its_real_state():
    """Bug: any exception (provider down, schema failure) escaped ainvoke —
    no checker, no summary, and the report used the INITIAL state."""
    async def planner(state):
        if state["step"] == 3:
            raise RuntimeError("provider exploded")
        return _res(element_id=0)

    app = build_graph(_env(), planner, _ok, approval=False)
    final = asyncio.run(run_graph(app, initial_state("g"), recursion_limit=300))
    assert terminal_reason(final) == "error"
    assert final["step"] == 3, "must report the steps actually taken, not 0"
    assert "provider exploded" in final["last_reason"]


def test_ctrl_c_ends_the_run_with_its_real_state():
    """Bug: asyncio.run() delivers Ctrl+C by CANCELLING the main task, which
    arrives as CancelledError — `except KeyboardInterrupt` never fired. This
    does what asyncio.run's SIGINT handler does: cancel the outer task.
    (Checked separately against a real SIGINT: same result.)"""
    outer: dict = {}

    async def planner(state):
        if state["step"] == 2:
            outer["task"].cancel()
            await asyncio.sleep(1)
        return _res(element_id=0)

    async def main():
        app = build_graph(_env(), planner, _ok, approval=False)
        outer["task"] = asyncio.current_task()
        return await run_graph(app, initial_state("g"), recursion_limit=300)

    final = asyncio.run(main())
    assert terminal_reason(final) == "interrupted"
    assert final["step"] == 2


def test_a_normal_run_is_unchanged():
    seq = iter([_res(element_id=0), _res(element_id=1), _res("done")])

    async def planner(state):
        return next(seq)

    app = build_graph(_env(), planner, _ok, approval=False)
    final = asyncio.run(run_graph(app, initial_state("g"), recursion_limit=300))
    assert terminal_reason(final) == "done" and final["step"] == 3


# -- verify: ids renumber between before and after --------------------------------
def test_text_check_follows_the_element_not_its_old_number():
    """Bug: expect.element_id (a BEFORE id) was looked up in the AFTER list.
    One new element above the field and the check read its neighbour."""
    before = [Element(0, "button", "Save", (0, 0, 40, 20)),
              Element(1, "textarea", "", (0, 100, 400, 300))]
    after = [Element(0, "button", "Save", (0, 0, 40, 20)),
             Element(1, "text", "Autosaved", (0, 40, 100, 60)),  # new: takes id 1
             Element(2, "textarea", "Reviewed", (0, 100, 400, 300))]
    outcome, why = check(Expectation(kind="text_in_element", element_id=1, value="Reviewed"),
                         before_elements=before, after_elements=after)
    assert outcome == "success", why


def test_a_text_area_that_grew_is_still_found():
    before = [Element(5, "textarea", "", (0, 100, 400, 300))]
    after = [Element(5, "textarea", "hello", (0, 100, 400, 320))]
    outcome, _ = check(Expectation(kind="text_in_element", element_id=5, value="hello"),
                       before_elements=before, after_elements=after)
    assert outcome == "success"


def test_typed_words_are_not_an_error_dialog():
    """Bug: typing 'error handling' into a document read as an error dialog."""
    assert error_dialog([Element(0, "textarea", "fix the error handling", (0, 0, 9, 9))]) is None
    assert error_dialog([Element(0, "text", "Save failed", (0, 0, 9, 9))]) == "Save failed"


# -- policy: a text field's name is its content -------------------------------------
def test_a_document_mentioning_send_does_not_need_approval_to_click():
    """Bug: a document containing 'send' or 'delete' made every click into it
    need approval, and the terminal prompt then stole focus (A5)."""
    doc = [Element(0, "textarea", "please send the report", (0, 0, 400, 300))]
    assert policy.requires_approval(Action(kind="click", element_id=0), doc,
                                    approval_on=False, risk_threshold="off") is None
    button = [Element(0, "button", "Send", (0, 0, 40, 20))]
    assert policy.requires_approval(Action(kind="click", element_id=0), button,
                                    approval_on=False, risk_threshold="off")


# -- executor: a wait is bounded ------------------------------------------------------
def test_wait_is_capped(monkeypatch):
    slept = []
    monkeypatch.setattr(executor_mod.time, "sleep", slept.append)

    class Ad:
        def frontmost_app(self): return ("x", "x")

    Executor(Ad(), approval_on=False).run([Action(kind="wait", amount=600_000)], [],
                                          mode="freeform", allowed_bundles=None)
    assert slept[0] == MAX_WAIT_MS / 1000


@pytest.mark.parametrize("kind", ["drag"])
def test_removed_action_kinds_are_refused_not_crashed(kind, monkeypatch):
    monkeypatch.setattr(executor_mod, "SETTLE_S", 0)

    class Ad:
        def frontmost_app(self): return ("x", "x")
    from os_agent.env.base import ActionError
    with pytest.raises(ActionError):
        Executor(Ad(), approval_on=False).run([Action(kind=kind, element_id=0)],
                                              fake_elements(1), mode="freeform",
                                              allowed_bundles=None)
