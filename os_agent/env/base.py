"""SEAM #1 — the task-level contract.

Everything the agent can do to the world is one of these five calls
(CLAUDE.md §7, §11). Phase B replaces the agent loop entirely; this layer
must not move when it does.

ASYNC: reset/observe/act are async so that observe() can run the screen
capture and the accessibility walk concurrently (A2). The adapter beneath is
sync — pyobjc and pyautogui are blocking C calls, and wrapping those in
`async def` would buy nothing. Concurrency where it pays, no async theatre
where it doesn't.
"""

from abc import ABC, abstractmethod

from os_agent.types import Action, Observation


class Environment(ABC):
    @abstractmethod
    async def reset(self, task_id: str) -> Observation:
        """Teardown, rebuild the sandbox, run setup, return the first observation."""

    @abstractmethod
    async def observe(self) -> Observation:
        """Capture screenshot + elements + annotated overlay."""

    @abstractmethod
    async def act(self, actions: list[Action]) -> None:
        """Execute in order. A LIST, never a single action — this is what makes
        Phase B's action grouping a zero-interface-change optimization (§4.1)."""

    @abstractmethod
    def evaluate(self) -> float:
        """Run the task's checker against END STATE only. Returns 0.0 or 1.0."""

    @abstractmethod
    def close(self) -> None: ...


class PolicyViolation(RuntimeError):
    """An action the guardrails refused. Never caught and retried — it means
    the agent tried something it is not allowed to do (CLAUDE.md §8.6)."""


class ApprovalDenied(RuntimeError):
    """A human said no at the approval prompt."""
