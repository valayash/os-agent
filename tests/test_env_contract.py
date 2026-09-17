"""Both Environments must return the SAME meta contract.

This file exists because of a bug that every other test was structurally
unable to see. `screen_hash` drives loop detection. replay_env set it;
DesktopEnv never did. All 44 tests passed, because all 44 run through
replay_env — the only path where the field existed.

A seam that lets you test the graph without a machine will also let the
machine-side implementation drift away from the tested one. The defence is to
test the CONTRACT rather than one implementation of it: assert the key set,
using a fake adapter so this still needs no real screen.
"""

import asyncio
import io

import pytest
from PIL import Image

from os_agent.env.desktop_env import DesktopEnv
from os_agent.env.replay_env import ReplayEnv, Screen
from os_agent.types import Element, RawNode

# Every observation, from any backend, must carry these.
REQUIRED_META = {"app", "bundle_id", "scale", "screen_hash"}


class FakeAdapter:
    """A desktop that is not a desktop. Enough for the contract, no screen."""

    name = "fake"

    def screen_size_points(self): return (1440, 900)
    def capture(self):
        # A REAL png: som.annotate() opens it, so a fake header is not enough.
        buf = io.BytesIO()
        Image.new("RGB", (1440, 900), "white").save(buf, format="PNG")
        return (buf.getvalue(), 2.0)
    def frontmost_app(self): return ("Fake", "com.fake.app")
    def raw_tree(self):
        return [RawNode(role="AXButton", name="Send", bbox=(10, 10, 60, 30))]


def _meta(env) -> dict:
    return asyncio.run(env.observe()).meta


def test_desktop_env_meta_satisfies_the_contract():
    meta = _meta(DesktopEnv(FakeAdapter(), mode="freeform"))
    assert REQUIRED_META <= set(meta), f"missing: {REQUIRED_META - set(meta)}"
    assert meta["screen_hash"], "screen_hash must not be empty — loop detection reads it"


def test_replay_env_meta_satisfies_the_contract():
    screen = Screen(app="A", bundle_id="com.a",
                    elements=[Element(id=0, role="button", name="Send",
                                      bbox=(10, 10, 60, 30))])
    meta = _meta(ReplayEnv([screen]))
    assert REQUIRED_META <= set(meta), f"missing: {REQUIRED_META - set(meta)}"
    assert meta["screen_hash"]


def test_both_envs_agree_on_the_fingerprint_of_one_screen():
    """The same elements must hash the same wherever they came from."""
    els = [Element(id=0, role="button", name="Send", bbox=(10, 10, 60, 30))]
    desktop = _meta(DesktopEnv(FakeAdapter(), mode="freeform"))["screen_hash"]
    replay = _meta(ReplayEnv([Screen(app="A", bundle_id="com.a", elements=els)]))["screen_hash"]
    assert desktop == replay, "two implementations of one fact have drifted again"


@pytest.mark.parametrize("name", ["Send", "Sent"])
def test_fingerprint_changes_when_the_screen_does(name):
    """A hash that never changes would disable loop detection just as well."""
    from os_agent.perception.elements import screen_fingerprint
    base = screen_fingerprint([Element(id=0, role="button", name="Send", bbox=(0, 0, 9, 9))])
    other = screen_fingerprint([Element(id=0, role="button", name=name, bbox=(0, 0, 9, 9))])
    assert (base == other) is (name == "Send")
