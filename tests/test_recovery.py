"""A bad action must cost a STEP, not the RUN.

Three failures used to end a run outright, and all three are ordinary:

    stale element_id   ids renumber on EVERY observation, so the planner
                       referring to one it read last screen is routine
    bad key name       press_keys(["cmd"]) / (["f5"]) raise ValueError from
                       the adapter, which nothing caught at all — the
                       traceback escaped ainvoke and the run ended with no
                       summary and no trajectory
    newline in `type`  refused since the eight-message incident

Each is now an ActionError: outcome=error, reason handed to the planner,
consecutive_failures incremented, run continues.
"""

import pytest

from os_agent.actions.executor import Executor
from os_agent.env.base import ActionError, PolicyViolation
from os_agent.types import Action, Element

ELEMENTS = [Element(id=0, role="button", name="Send", bbox=(0, 0, 40, 20))]


class _Adapter:
    name = "fake"

    def __init__(self):
        self.clicks, self.keys, self.typed = [], [], []

    def frontmost_app(self): return ("Test", "com.test")
    def screen_size_points(self): return (1440, 900)
    def click(self, x, y, kind="click"): self.clicks.append((x, y))
    def type_text(self, t): self.typed.append(t)

    def press_keys(self, keys):
        # The real macOS adapter's contract, reproduced exactly.
        names = [k.lower() for k in keys]
        plain = [n for n in names if n not in ("command", "shift", "option", "control")]
        if len(plain) != 1:
            raise ValueError(f"press_keys needs exactly one non-modifier key, got {keys!r}")
        if plain[0] not in ("enter", "s", "space"):
            raise ValueError(f"no macOS keycode for {plain[0]!r}")
        self.keys.append(keys)


def _exec(adapter=None):
    return Executor(adapter or _Adapter(), approve=lambda *_: True, approval_on=False)


def _run(ex, action):
    ex.run([action], ELEMENTS, mode="freeform", allowed_bundles=None)


@pytest.mark.parametrize("action,needle", [
    (Action(kind="click", element_id=99), "not on screen"),
    (Action(kind="click", coords=(5000, 5000)), "outside"),
    (Action(kind="key", keys=["command"]), "non-modifier"),
    (Action(kind="key", keys=["f5"]), "keycode"),
    (Action(kind="type", text="hi\n"), "newline"),
    (Action(kind="drag", element_id=0), "no handler"),
    (Action(kind="type"), "no text"),
])
def test_recoverable_failures_raise_ActionError_not_PolicyViolation(action, needle):
    with pytest.raises(ActionError, match=needle):
        _run(_exec(), action)


def test_an_adapter_ValueError_does_not_escape_as_itself():
    """It used to propagate out of the graph and kill the process."""
    with pytest.raises(ActionError):
        _run(_exec(), Action(kind="key", keys=["f5"]))


def test_a_guardrail_refusal_is_still_terminal():
    """Recovery must not have softened the line §8.6 draws."""
    with pytest.raises(PolicyViolation, match="allowlist"):
        _exec().run([Action(kind="click", element_id=0)], ELEMENTS,
                    mode="bench", allowed_bundles=["com.somethingelse"])


def test_the_executor_keeps_working_after_a_recoverable_failure():
    """The point of the whole change: the next action still runs."""
    adapter = _Adapter()
    ex = _exec(adapter)
    with pytest.raises(ActionError):
        _run(ex, Action(kind="click", element_id=99))
    _run(ex, Action(kind="click", element_id=0))
    assert adapter.clicks == [(20, 10)]


# --- the same thing, end to end through the graph --------------------------

@pytest.mark.asyncio
async def test_a_recoverable_failure_does_not_end_the_run():
    """A stale id used to terminate with terminal_reason='policy' at step 0.

    Driven through replay_env so the failure is injected precisely: the env
    raises ActionError on the first act() and succeeds afterwards. A healthy
    agent absorbs that and reaches `done`.
    """
    from os_agent.agent.graph import build_graph, terminal_reason
    from os_agent.agent.state import initial_state
    from os_agent.env.replay_env import ReplayEnv, Screen, fake_elements
    from os_agent.llm.base import LLMResult
    from os_agent.types import Expectation, PlannedAction

    class FlakyEnv(ReplayEnv):
        def __init__(self, screens):
            super().__init__(screens)
            self.calls = 0

        async def act(self, actions):
            self.calls += 1
            if self.calls == 1:
                raise ActionError("element_id 99 is not on screen (ids present: [0, 1])")
            await super().act(actions)

    def planned(kind, **kw):
        return PlannedAction(reasoning="r", subgoal="s", action=Action(kind=kind, **kw),
                             expect=Expectation(kind="screen_changed"), confidence=0.5)

    seq = [planned("click", element_id=99), planned("click", element_id=0),
           planned("done")]

    async def planner(state):
        p = seq[min(state["step"], len(seq) - 1)]
        return LLMResult(parsed=p, prompt_tokens=0, completion_tokens=0,
                         latency_ms=0.0, provider_wait_ms=0.0, cost_usd=0.0,
                         repairs=0, model="stub", provider="stub")

    async def verifier(state, action, expect, after):
        if state.get("executor_error"):
            return "error", state["executor_error"]
        return "success", "ok"

    env = FlakyEnv([Screen(elements=fake_elements(3))])
    app = build_graph(env, planner, verifier, approval=False)
    final = await app.ainvoke(initial_state("g"), config={"recursion_limit": 60})

    assert terminal_reason(final) == "done", "a stale id must not end the run"
    assert final["step"] >= 2, "it should have kept going after the bad action"
