"""The Jev / choice backend (CLAUDE.md §19) — everything testable without a key.

What these prove: the questions offered match the action space of the mode,
answers decode to the PlannedAction the graph already runs, nothing outside
the offered set executes, typing costs a counted second call, a sparse screen
goes to the vision planner, and the HTTP client accounts for its waiting.

What they cannot prove: that TypeSafe's live API accepts this exact request.
The shape is the reference repo's, and no call has been made from here.
"""

import asyncio
import json

import httpx
import pytest

from os_agent import run
from os_agent.actions import policy
from os_agent.actions.executor import Executor
from os_agent.agent import choice
from os_agent.agent.state import initial_state
from os_agent.env.base import ActionError
from os_agent.llm import jev_client
from os_agent.llm.base import LLMResult
from os_agent.llm.jev_client import JevAnswers, JevClient, JevError
from os_agent.telemetry.tracer import Tracer
from os_agent.types import Element, MenuCommand, PlannedAction, TextValue


def els():
    return [
        Element(0, "button", "Save", (0, 0, 40, 20), ops=("press",)),
        Element(1, "textfield", "Title", (0, 30, 200, 50), ops=("press", "type")),
        Element(2, "button", "Cancel", (50, 0, 90, 20), ops=("press",)),
    ]


def state(**kw):
    st = initial_state("rename the title to Q3")
    st["elements"] = els()
    st.update(kw)
    return st


def ans(op, conf=0.9, risk=0.0, **targets):
    a = {"operation": {"choice": op, "confidence": conf,
                       "probabilities": {op: conf}},
         "risk": {"score": risk, "probabilities": {}}}
    for head, choice_ in targets.items():
        a[f"{head}_target"] = {"choice": choice_}
    return a


# -- the questions --------------------------------------------------------------
def test_synthetic_mode_offers_no_ax_operations():
    req = choice.build_request(state())
    assert "set_value" not in req.operations and "menu" not in req.operations
    assert {"click", "type", "key", "scroll_down", "done", "fail"} <= set(req.operations)
    assert req.questions["click_target"]["criteria"]["1"] == "textfield 'Title' {press,type}"


def test_ax_mode_offers_set_value_only_on_writable_elements():
    req = choice.build_request(state(), exec_mode="ax")
    assert set(req.questions["set_value_target"]["criteria"]) == {"1"}


def test_menu_is_offered_only_when_enabled():
    st = state(menu=[MenuCommand(0, "Format > Make Rich Text", "m")])
    assert "menu" not in choice.build_request(st).operations
    req = choice.build_request(st, exec_mode="ax", menu_actions=True)
    assert req.questions["menu_target"]["criteria"] == {"0": "Format > Make Rich Text"}


def test_dangerous_quit_and_close_are_not_offered_as_keys():
    keys = choice.build_request(state()).questions["key_target"]["criteria"]
    assert "cmd+q" not in keys and "cmd+w" not in keys and "cmd+s" in keys


def test_state_carries_no_history():
    req = choice.build_request(state(step=40))
    assert set(req.state) <= {"goal", "app", "elements", "last_action", "step",
                              "menu_commands", "facts", "advice_after_getting_stuck",
                              "table_is_incomplete"}


# -- decoding ---------------------------------------------------------------------
def test_click_decodes_to_the_chosen_element_with_measured_confidence():
    req = choice.build_request(state())
    planned, needs_text, trace = choice.decode(ans("click", 0.83, click="2"), req,
                                               elements=els())
    assert (planned.action.kind, planned.action.element_id) == ("click", 2)
    assert planned.confidence == pytest.approx(0.83)
    assert not needs_text
    assert trace["operation_probabilities"] == {"click": 0.83}


def test_an_operation_that_was_not_offered_runs_nothing():
    req = choice.build_request(state())
    with pytest.raises(choice.ChoiceRefused):
        choice.decode(ans("set_value", set_value="1"), req, elements=els())


def test_a_target_that_was_not_offered_is_refused_recoverably():
    req = choice.build_request(state())
    planned, _, _ = choice.decode(ans("click", click="99"), req, elements=els())
    assert planned.action.element_id is None
    v = policy.check(planned.action, els(), mode="freeform", frontmost_bundle="x",
                     allowed_bundles=None, approval_on=False)
    assert not v.allowed and v.recoverable


def test_key_choice_becomes_a_chord():
    req = choice.build_request(state())
    planned, _, _ = choice.decode(ans("key", key="cmd+s"), req, elements=els())
    assert planned.action.keys == ["command", "s"]


@pytest.mark.parametrize("score,level", [(0, "low"), (0.4, "low"), (1, "medium"),
                                         (1.4, "medium"), (2, "high"), (None, "high")])
def test_risk_score_maps_to_tiers_and_unreadable_is_high(score, level):
    assert choice.risk_level(score) == level


def test_high_risk_choice_needs_a_human():
    req = choice.build_request(state())
    planned, _, _ = choice.decode(ans("click", risk=2, click="0"), req, elements=els())
    assert planned.action.risk == "high"
    assert policy.requires_approval(planned.action, els(), approval_on=False)


def test_typing_needs_text_and_gets_a_strong_expectation():
    req = choice.build_request(state(), exec_mode="ax")
    planned, needs_text, _ = choice.decode(ans("set_value", set_value="1"), req,
                                           elements=els())
    assert needs_text and planned.action.text is None
    filled = choice.with_text(planned, "Q3", els())
    assert filled.action.text == "Q3"
    assert (filled.expect.kind, filled.expect.element_id) == ("text_in_element", 1)


def test_text_model_declining_types_nothing(monkeypatch):
    from os_agent.actions import executor as ex_mod
    monkeypatch.setattr(ex_mod, "SETTLE_S", 0)
    req = choice.build_request(state())
    planned, _, _ = choice.decode(ans("type"), req, elements=els())
    planned = choice.with_text(planned, None, els())

    class Ad:
        def frontmost_app(self): return ("x", "x")
        def type_text(self, t): raise AssertionError("typed on a guess")
    with pytest.raises(ActionError, match="no text"):
        Executor(Ad(), approval_on=False).run([planned.action], els(), mode="freeform",
                                              allowed_bundles=None)


def test_newlines_from_the_text_model_cannot_become_an_enter():
    req = choice.build_request(state())
    planned, _, _ = choice.decode(ans("type"), req, elements=els())
    assert "\n" not in choice.with_text(planned, "a\nb", els()).action.text


# -- the HTTP client ----------------------------------------------------------------
@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(jev_client, "BASE_BACKOFF_S", 0.0)


def client(handler):
    return JevClient("jev/jev-latest", base_url="https://jev.test/v1", api_key="k",
                     rpm=0, transport=httpx.MockTransport(handler))


def test_request_shape_and_answer(no_sleep):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answers": ans("done"), "model": "jev-latest",
                                         "usage": {"input_tokens": 300, "output_tokens": 4}})

    r = client(handler).plan(system="", text=json.dumps({"goal": "g"}), image_png=b"",
                             schema=JevAnswers, extra={"questions": {"operation": {}}})
    assert seen["url"] == "https://jev.test/v1/systemone"
    assert seen["auth"] == "Bearer k"
    assert seen["body"] == {"model": "jev-latest", "state": {"goal": "g"},
                            "questions": {"operation": {}}}
    assert (r.prompt_tokens, r.completion_tokens, r.provider) == (300, 4, "jev")
    assert r.parsed.answers["operation"]["choice"] == "done"


def test_retry_time_is_provider_wait_not_latency(no_sleep):
    calls = iter([httpx.Response(503), httpx.Response(200, json={"answers": ans("done")})])
    r = client(lambda req: next(calls)).plan(system="", text="{}", image_png=b"",
                                             schema=JevAnswers, extra={"questions": {}})
    assert r.raw["attempts"] == 2 and r.provider_wait_ms > 0


def test_a_hard_error_names_the_status_and_not_the_key(no_sleep):
    with pytest.raises(JevError) as e:
        client(lambda req: httpx.Response(401, text="bad key")).plan(
            system="", text="{}", image_png=b"", schema=JevAnswers, extra={"questions": {}})
    assert "401" in str(e.value) and "Bearer" not in str(e.value)


def test_an_image_is_refused_rather_than_silently_dropped():
    with pytest.raises(JevError, match="text only"):
        client(lambda r: httpx.Response(200)).plan(system="", text="{}", image_png=b"x",
                                                   schema=JevAnswers, extra={"questions": {}})


# -- the planner routing ------------------------------------------------------------
def _result(parsed, model="m", provider="p"):
    return LLMResult(parsed=parsed, prompt_tokens=10, completion_tokens=2, latency_ms=5.0,
                     provider_wait_ms=0.0, cost_usd=0.001, repairs=0, model=model,
                     provider=provider, raw={"attempts": 1})


class FakeJev:
    model, provider = "jev/jev-latest", "jev"

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def plan(self, **kw):
        self.calls += 1
        return _result(JevAnswers(answers=self.answers), self.model, self.provider)


class FakeText:
    model, provider = "gemini/gemini-3.1-flash-lite", "gemini"

    def __init__(self):
        self.calls = 0

    def plan(self, **kw):
        self.calls += 1
        return _result(TextValue(text="Q3"), self.model, self.provider)


async def _fallback(st):
    raise AssertionError("fallback should not run")


def test_a_click_step_is_one_call():
    jev, text, tr = FakeJev(ans("click", click="0")), FakeText(), Tracer()
    r = asyncio.run(run.make_choice_planner(jev, text, _fallback, tr)(state()))
    assert r.raw["calls"] == 1 and text.calls == 0 and tr.calls == 1
    assert isinstance(r.parsed, PlannedAction) and r.parsed.action.element_id == 0


def test_a_typing_step_is_two_calls_and_says_so():
    jev, text, tr = FakeJev(ans("type")), FakeText(), Tracer()
    r = asyncio.run(run.make_choice_planner(jev, text, _fallback, tr)(state()))
    assert r.raw["calls"] == 2 and tr.calls == 2
    assert r.parsed.action.text == "Q3"
    assert r.cost_usd == pytest.approx(0.002)


def test_a_sparse_screen_goes_to_the_vision_planner():
    called = {}

    async def fallback(st):
        called["yes"] = True
        return "vision"

    jev = FakeJev(ans("done"))
    out = asyncio.run(run.make_choice_planner(jev, FakeText(), fallback, Tracer())(
        state(elements=els()[:1])))
    assert out == "vision" and called and jev.calls == 0
