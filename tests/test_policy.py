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
