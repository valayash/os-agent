"""The AX execution path (CLAUDE.md §4.11) — everything that can be tested
without a Mac.

What a fake adapter CAN prove: the guard refuses a node that changed, a timeout
ends the run instead of retrying, a write that did not stick is an error, the
synthetic path refuses AX actions, capabilities become ops, menu filtering
and drops the dangerous and the unstable.

What it CANNOT prove: that AXPress needs no focus on this machine. That is
scripts/ax_background.py, and until it has run, §5.3 stands.
"""

import asyncio
import io

import pytest
from PIL import Image

from os_agent.actions import executor as executor_mod
from os_agent.actions import policy
from os_agent.actions.executor import Executor, UncertainDispatch
from os_agent.agent.prompts import SYSTEM, build_user, format_elements, system_prompt
from os_agent.agent.state import initial_state
from os_agent.env.base import ActionError
from os_agent.env.desktop_env import DesktopEnv
from os_agent.perception.elements import MENU_CAP, ops_of, to_elements, to_menu
from os_agent.types import (
    Action,
    Element,
    MenuCommand,
    RawNode,
    Receipt,
)

APP = "com.fake.app"


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(executor_mod, "SETTLE_S", 0.0)


class AXFake:
    """A desktop whose AX layer records what was asked of it."""

    name = "fake"

    def __init__(self, nodes=None, *, value_sticks=True, timeout=False):
        self.nodes = {n.path: n for n in (nodes or [])}
        self.values: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.value_sticks = value_sticks
        self.timeout = timeout

    def screen_size_points(self): return (1440, 900)
    def capture(self):
        buf = io.BytesIO()
        Image.new("RGB", (1440, 900), "white").save(buf, format="PNG")
        return buf.getvalue(), 2.0
    def frontmost_app(self): return ("Fake", APP)
    def raw_tree(self): return list(self.nodes.values())
    def menu_tree(self):
        return [RawNode(role="AXMenuItem", name="Format > Make Rich Text", bbox=None,
                        path="m:1:Format\tMake Rich Text")]
    def click(self, x, y, kind="click"): self.calls.append(("click", x, y))
    def type_text(self, text): self.calls.append(("type", text))
    def press_keys(self, keys): self.calls.append(("keys", tuple(keys)))
    def scroll(self, x, y, amount): self.calls.append(("scroll", amount))

    def read_node(self, path):
        if path.startswith("m:"):
            return RawNode(role="AXMenuItem", name="Format > Make Rich Text",
                           bbox=None, path=path)
        return self.nodes.get(path)

    def read_value(self, path): return self.values.get(path)

    def press(self, path):
        self.calls.append(("press", path))
        if self.timeout:
            return Receipt("ax_press", dispatched=True, timed_out=True, detail="no reply")
        return Receipt("ax_press", dispatched=True, window_changed=True)

    def set_value(self, path, text):
        self.calls.append(("set_value", path, text))
        self.values[path] = text if self.value_sticks else "old" + text
        ok = self.values[path] == text
        return Receipt("ax_set_value", dispatched=True, verified=ok,
                       detail="" if ok else "read back differs")

    def invoke_menu(self, path):
        self.calls.append(("menu", path))
        return Receipt("ax_menu", dispatched=True)


BUTTON = RawNode(role="AXButton", name="Bold", bbox=(10, 10, 60, 30),
                 actions=("AXPress",), path="w:1:0/1")
FIELD = RawNode(role="AXTextField", name="Title", bbox=(10, 50, 200, 70),
                actions=("AXConfirm",), value_settable=True, path="w:1:0/2")
LABEL = RawNode(role="AXStaticText", name="Hello", bbox=(10, 90, 200, 110),
                path="w:1:0/3")


def run(ex: Executor, action: Action, elements, menu=None):
    ex.run([action], elements, mode="freeform", allowed_bundles=None, menu=menu)


def els():
    return to_elements([BUTTON, FIELD, LABEL])


# -- step 2: capabilities -----------------------------------------------------
def test_capabilities_become_ops():
    by_name = {e.name: e for e in els()}
    assert by_name["Bold"].ops == ("press",)
    assert by_name["Title"].ops == ("press", "type")
    assert by_name["Hello"].ops == ()
    assert by_name["Bold"].path == "w:1:0/1"


def test_a_settable_checkbox_is_not_a_place_to_type():
    assert ops_of("checkbox", ("AXPress",), True) == ("press",)


def test_ops_render_in_the_prompt_only_when_known():
    text = format_elements(els())
    assert "'Bold' {press}" in text
    assert "'Title' {press,type}" in text
    assert "'Hello'\n" in text + "\n"  # no braces for unknown ops


# -- step 3: guarded AX execution ----------------------------------------------
def test_ax_mode_presses_instead_of_clicking():
    ad = AXFake([BUTTON, FIELD])
    e = els()
    run(Executor(ad, approval_on=False, exec_mode="ax"),
        Action(kind="click", element_id=e[0].id), e)
    assert ad.calls == [("press", "w:1:0/1")]


def test_synthetic_mode_never_touches_ax():
    ad = AXFake([BUTTON])
    e = to_elements([BUTTON])
    run(Executor(ad, approval_on=False, exec_mode="synthetic"),
        Action(kind="click", element_id=0), e)
    assert ad.calls == [("click", 35, 20)]


def test_synthetic_mode_refuses_ax_only_actions_recoverably():
    ad = AXFake([FIELD])
    e = to_elements([FIELD])
    with pytest.raises(ActionError, match="EXEC_MODE=ax"):
        run(Executor(ad, approval_on=False, exec_mode="synthetic"),
            Action(kind="set_value", element_id=0, text="x"), e)
    assert ad.calls == []


def test_guard_refuses_a_node_that_changed_under_the_handle():
    # Observed "Bold"; by the time we act, the same path is "Clear".
    ad = AXFake([RawNode(role="AXButton", name="Clear", bbox=(10, 10, 60, 30),
                         actions=("AXPress",), path="w:1:0/1")])
    e = to_elements([BUTTON])
    with pytest.raises(ActionError, match="changed since it was observed"):
        run(Executor(ad, approval_on=False, exec_mode="ax"),
            Action(kind="click", element_id=0), e)
    assert not any(c[0] == "press" for c in ad.calls)


def test_guard_refuses_a_node_that_is_gone():
    ad = AXFake([])
    with pytest.raises(ActionError, match="is gone"):
        run(Executor(ad, approval_on=False, exec_mode="ax"),
            Action(kind="click", element_id=0), to_elements([BUTTON]))


def test_no_reply_ends_the_run_rather_than_retrying():
    ad = AXFake([BUTTON], timeout=True)
    with pytest.raises(UncertainDispatch, match="MAY have run"):
        run(Executor(ad, approval_on=False, exec_mode="ax"),
            Action(kind="click", element_id=0), to_elements([BUTTON]))


def test_set_value_that_did_not_stick_is_an_error():
    ad = AXFake([FIELD], value_sticks=False)
    with pytest.raises(ActionError, match="did not stick"):
        run(Executor(ad, approval_on=False, exec_mode="ax"),
            Action(kind="set_value", element_id=0, text="Q3"), to_elements([FIELD]))


def test_set_value_writes_and_records_the_receipt():
    ad = AXFake([FIELD])
    ex = Executor(ad, approval_on=False, exec_mode="ax")
    run(ex, Action(kind="set_value", element_id=0, text="Q3"), to_elements([FIELD]))
    assert ad.values["w:1:0/2"] == "Q3"
    assert [r.mechanism for r in ex.receipts] == ["ax_set_value"]


def test_set_value_refused_on_an_element_without_type():
    ad = AXFake([BUTTON])
    with pytest.raises(ActionError, match="does not accept a written value"):
        run(Executor(ad, approval_on=False, exec_mode="ax"),
            Action(kind="set_value", element_id=0, text="x"), to_elements([BUTTON]))


def test_no_press_action_falls_back_to_a_real_click_and_says_so():
    plain = RawNode(role="AXButton", name="Odd", bbox=(10, 10, 60, 30), path="w:1:0/9")
    ad = AXFake([plain])
    ex = Executor(ad, approval_on=False, exec_mode="ax")
    run(ex, Action(kind="click", element_id=0), to_elements([plain]))
    assert ad.calls == [("click", 35, 20)]
    assert ex.receipts[0].mechanism == "synthetic_click"


# -- step 4: menu action space ----------------------------------------------------
def _m(title):
    return RawNode(role="AXMenuItem", name=title, bbox=None, path=f"m:1:{title}")


def test_menu_filter_drops_fullscreen_flips_and_the_apple_menu():
    titles = [c.title for c in to_menu([
        _m("Format > Make Rich Text"),
        _m("View > Enter Full Screen"),
        _m("View > Show Ruler"),
        _m("Apple > Restart…"),
        _m("File"),  # a bar item alone is not a command
        _m("Format > Make Rich Text"),  # duplicate
    ])]
    assert titles == ["Format > Make Rich Text"]


def test_menu_is_capped_because_it_enters_the_prompt():
    assert len(to_menu([_m(f"Edit > Item {i}") for i in range(500)])) == MENU_CAP


def test_menu_name_is_not_the_command():
    # "Format" is the MENU, not "format a disk". Only the leaf is matched.
    menu = [MenuCommand(id=0, title="Format > Make Rich Text", path="m:1:x")]
    assert policy.requires_approval(Action(kind="menu", element_id=0), [],
                                    approval_on=False, menu=menu,
                                    risk_threshold="off") is None


def test_menu_command_invokes_with_one_action():
    ad = AXFake([])
    menu = [MenuCommand(id=0, title="Format > Make Rich Text",
                        path="m:1:Format\tMake Rich Text")]
    run(Executor(ad, approval_on=False, exec_mode="ax"),
        Action(kind="menu", element_id=0), [], menu)
    assert ad.calls == [("menu", "m:1:Format\tMake Rich Text")]


def test_irreversible_menu_titles_still_need_a_human():
    menu = [MenuCommand(id=3, title="File > Move to Trash", path="m:1:x")]
    r = policy.requires_approval(Action(kind="menu", element_id=3), [], approval_on=False,
                                 menu=menu, risk_threshold="off")
    assert r and "move to trash" in r


def test_desktop_env_carries_the_menu_into_the_observation():
    env = DesktopEnv(AXFake([BUTTON]), mode="freeform", exec_mode="ax", menu_actions=True)
    obs = asyncio.run(env.observe())
    assert [m.title for m in obs.menu] == ["Format > Make Rich Text"]
    assert obs.meta["exec_mode"] == "ax"


def test_menu_enters_the_prompt_as_mN():
    st = initial_state("g")
    st["menu"] = [MenuCommand(id=7, title="Format > Make Rich Text", path="")]
    assert "[m7] Format > Make Rich Text" in build_user(st)


# -- step 6: risk --------------------------------------------------------
def test_high_risk_needs_a_human_even_with_yolo():
    r = policy.requires_approval(Action(kind="click", element_id=0, risk="high"),
                                 [Element(0, "button", "OK", (0, 0, 9, 9))],
                                 approval_on=False, risk_threshold="high")
    assert r and "risk=high" in r


def test_risk_never_overrides_irreversible():
    # The model calling "Send" low risk does not get past the label match.
    r = policy.requires_approval(Action(kind="click", element_id=0, risk="low"),
                                 [Element(0, "button", "Send", (0, 0, 9, 9))],
                                 approval_on=False, risk_threshold="high")
    assert r and "send" in r


def test_set_value_in_a_filename_field_is_a_rename():
    field = [Element(0, "textfield", "notes.odt", (0, 0, 90, 20), ops=("type",))]
    r = policy.requires_approval(Action(kind="set_value", element_id=0, text="x"),
                                 field, approval_on=False, risk_threshold="off")
    assert r and "rename" in r
    r = policy.requires_approval(Action(kind="set_value", element_id=0, text="x"),
                                 [], approval_on=False, bundle="com.apple.finder",
                                 risk_threshold="off")
    assert r and "FILENAME" in r


def test_default_config_leaves_the_system_prompt_untouched():
    assert system_prompt() == SYSTEM
    full = system_prompt(exec_mode="ax", menu_actions=True)
    assert "set_value" in full and "[mN]" in full
