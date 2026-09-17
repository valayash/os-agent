"""A scripted Environment. No machine, no permissions, no Spaces (§6).

Two jobs:

1. Let the graph be tested in milliseconds and deterministically. Routing bugs
   — a cycle that never exits, an off-by-one step cap, loop detection that
   does not fire — are trivial to find when nothing else is moving and
   miserable to find when a real desktop and a real model are also involved.

2. Prove the Environment seam is real. If one interface hosts both a live Mac
   and a scripted replay, it is an abstraction rather than decoration. §2
   claims that; this is what tests the claim.

Later it also replays RECORDED trajectories (§4.6), which is how Phase B's
trajectory cache and speculation predictor get evaluated offline.
"""

from dataclasses import dataclass, field

from os_agent.env.base import Environment
from os_agent.perception.elements import screen_fingerprint
from os_agent.types import Action, Element, Observation


def fake_elements(n: int, *, offset: int = 0) -> list[Element]:
    """n plausible elements laid out in a column, in POINTS like everything else."""
    return [
        Element(
            id=i,
            role="button" if i % 3 else "textfield",
            name=f"widget-{offset + i}",
            bbox=(20, 40 + i * 30, 220, 66 + i * 30),
        )
        for i in range(n)
    ]


@dataclass
class Screen:
    """One scripted screen state."""

    app: str = "FakeApp"
    bundle_id: str = "com.example.fake"
    elements: list[Element] = field(default_factory=lambda: fake_elements(4))

    def fingerprint(self) -> str:
        """Stable identity of a screen, for loop detection.

        Uses Element.key() — role|name|bbox — rather than the positional id,
        for the same reason trajectories do (§4.7): ids renumber, so hashing
        them would make two identical screens look different.
        """
        return screen_fingerprint(self.elements)


class ReplayEnv(Environment):
    def __init__(self, screens: list[Screen] | None = None, *, loop: bool = True) -> None:
        # loop=True means the script repeats forever. That is deliberate: a
        # step-cap test needs an environment that never runs out of screens,
        # because "the script ended" is not one of the terminal conditions we
        # are trying to prove.
        self.screens = screens or [Screen()]
        self.loop = loop
        self.index = 0
        self.acted: list[list[Action]] = []
        self.score = 1.0
        self.closed = False

    # -- Environment --------------------------------------------------------
    async def reset(self, task_id: str) -> Observation:
        self.index = 0
        self.acted.clear()
        return await self.observe()

    async def observe(self) -> Observation:
        s = self.current
        png = b"\x89PNG\r\n\x1a\n" + s.fingerprint().encode()  # not a real image
        return Observation(
            screenshot=png,
            annotated=png,
            elements=s.elements,
            meta={
                "app": s.app,
                "bundle_id": s.bundle_id,
                "scale": 1.0,
                "screen_hash": s.fingerprint(),
                "replay_index": self.index,
            },
        )

    async def act(self, actions: list[Action]) -> None:
        self.acted.append(list(actions))
        self.advance()

    def evaluate(self) -> float:
        return self.score

    def close(self) -> None:
        self.closed = True

    # -- script control -----------------------------------------------------
    @property
    def current(self) -> Screen:
        return self.screens[self.index % len(self.screens)] if self.loop \
            else self.screens[min(self.index, len(self.screens) - 1)]

    def advance(self) -> None:
        self.index += 1
