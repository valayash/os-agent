"""Guardrails are safety code. They get real tests (CLAUDE.md §10)."""

import pytest

from os_agent.actions import policy
from os_agent.types import Action, Element

FINDER = "com.apple.finder"
TEXTEDIT = "com.apple.TextEdit"


def el(id_: int, name: str) -> Element:
    return Element(id=id_, role="button", name=name, bbox=(0, 0, 40, 20))


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Delete", "delete"),
        ("Move to Trash", "move to trash"),
        ("Send", "send"),
        ("Shut Down…", "shut down"),
        # word-boundary matching: these must NOT trip
        ("Undelete", None),
        ("Deleted Items", None),
        ("Resend later", None),
        ("Save", None),
    ],
)
def test_irreversible_word_boundaries(label, expected):
    assert policy.irreversible_hit(label) == expected


def test_app_allowlist_blocks_other_apps():
    v = policy.check(
        Action(kind="click", element_id=1),
        [el(1, "OK")],
        mode="bench",
        frontmost_bundle=FINDER,
        allowed_bundles=[TEXTEDIT],
    )
    assert not v.allowed
    assert "not in the task allowlist" in v.reason


def test_freeform_allows_any_app():
    assert policy.app_allowed(FINDER, None)
    assert not policy.app_allowed(FINDER, [TEXTEDIT])


def test_dangerous_chord_needs_approval():
    r = policy.requires_approval(Action(kind="key", keys=["command", "delete"]), [])
    assert r and "dangerous key chord" in r


def test_terminal_actions_bypass_everything():
    v = policy.check(
        Action(kind="done"), [], mode="bench",
        frontmost_bundle="anything", allowed_bundles=[TEXTEDIT],
    )
    assert v.allowed and not v.needs_approval


def test_click_with_no_target_rejected():
    v = policy.check(
        Action(kind="click"), [], mode="bench",
        frontmost_bundle=TEXTEDIT, allowed_bundles=[TEXTEDIT],
    )
    assert not v.allowed


def test_unknown_mode_rejected():
    v = policy.check(
        Action(kind="click", element_id=1), [el(1, "OK")], mode="yolo",
        frontmost_bundle=TEXTEDIT, allowed_bundles=[TEXTEDIT],
    )
    assert not v.allowed


def test_sandbox_confinement():
    assert policy.path_in_sandbox("~/os-agent-sandbox/notes.txt")
    assert policy.path_in_sandbox("~/os-agent-sandbox/deep/nested/a.txt")
    assert not policy.path_in_sandbox("~/Documents/taxes.pdf")
    assert not policy.path_in_sandbox("/etc/passwd")
    # the classic escape
    assert not policy.path_in_sandbox("~/os-agent-sandbox/../../.ssh/id_rsa")


# --- outward commits (2026-09-17: eight unwanted WhatsApp messages) --------

def test_newline_in_typed_text_is_an_outward_commit():
    """The hole that sent the messages: no label, no key, just a character."""
    assert policy.commits_outward(Action(kind="type", text="hi\nthere"))
    assert policy.commits_outward(Action(kind="key", keys=["enter"]))
    assert policy.commits_outward(Action(kind="type", text="hi there")) is None
    assert policy.commits_outward(Action(kind="click", element_id=3)) is None


def test_repeated_commit_needs_a_human_even_with_yolo():
    a = Action(kind="key", keys=["enter"])
    assert policy.requires_approval(a, [], approval_on=False, repeated=False) is None
    reason = policy.requires_approval(a, [], approval_on=False, repeated=True)
    assert reason and "already ran" in reason


def test_executor_refuses_a_newline_inside_type():
    from os_agent.actions.executor import Executor
    from os_agent.env.base import PolicyViolation

    class _Adapter:
        def frontmost_app(self): return ("Test", "com.test")
        def screen_size_points(self): return (1440, 900)
        def type_text(self, text): raise AssertionError("must never be reached")

    ex = Executor(_Adapter(), approve=lambda *_: True, approval_on=False)
    with pytest.raises(PolicyViolation, match="newline"):
        ex.run([Action(kind="type", text="hello\n")], [],
               mode="freeform", allowed_bundles=None)


def test_executor_caps_outward_commits_per_run():
    from os_agent.actions.executor import Executor
    from os_agent.env.base import PolicyViolation

    sent = []

    class _Adapter:
        def frontmost_app(self): return ("Test", "com.test")
        def screen_size_points(self): return (1440, 900)
        def press_keys(self, keys): sent.append(keys)

    ex = Executor(_Adapter(), approve=lambda *_: True, approval_on=False)
    # Distinct text each time so dedup cannot be what stops it — the CAP must.
    for i in range(policy.MAX_COMMITS_PER_RUN):
        ex.run([Action(kind="key", keys=["enter"], text=f"m{i}")], [],
               mode="freeform", allowed_bundles=None)
    assert len(sent) == policy.MAX_COMMITS_PER_RUN
    with pytest.raises(PolicyViolation, match="outward commit"):
        ex.run([Action(kind="key", keys=["enter"], text="one too many")], [],
               mode="freeform", allowed_bundles=None)
