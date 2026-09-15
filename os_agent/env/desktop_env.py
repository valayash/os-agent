"""ONE Environment for every platform (CLAUDE.md §6).

Parameterized by a DesktopAdapter, so adding Windows later means writing
desktop/windows.py and changing one line of config — not a second Environment.
"""

import asyncio
import time

import structlog

from os_agent.actions.executor import ApproveFn, Executor
from os_agent.bench import tasks
from os_agent.desktop.base import DesktopAdapter, NotOnThisPlatform
from os_agent.env.base import Environment
from os_agent.types import Action, Observation

log = structlog.get_logger(__name__)


class DesktopEnv(Environment):
    def __init__(
        self,
        adapter: DesktopAdapter,
        *,
        mode: str = "bench",
        approve: ApproveFn | None = None,
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.executor = Executor(adapter, approve=approve)
        self.task: tasks.TaskSpec | None = None

    # ------------------------------------------------------------------
    async def reset(self, task_id: str) -> Observation:
        self.task = tasks.get(task_id)
        log.info("env.reset", task=task_id, apps=self.task.apps)
        self.task.teardown()
        self.task.setup()
        await asyncio.sleep(1.5)  # let the app come up and take focus
        return await self.observe()

    async def observe(self) -> Observation:
        """Capture and perceive CONCURRENTLY.

        This is the whole reason Environment is async while the adapter is
        sync: the screen capture (~55 ms) and the accessibility walk (A2,
        expected 100-300 ms) have no dependency on each other.
        """
        t0 = time.perf_counter()
        png_task = asyncio.to_thread(self.adapter.capture)
        tree_task = asyncio.to_thread(self._safe_tree)
        (png, ratio), nodes = await asyncio.gather(png_task, tree_task)

        name, bundle = self.adapter.frontmost_app()
        elements = []  # A2: perception/elements.py turns nodes -> Elements
        perception_ms = (time.perf_counter() - t0) * 1000

        return Observation(
            screenshot=png,
            annotated=png,  # A2: som.py draws the numbered boxes
            elements=elements,
            meta={
                "app": name,
                "bundle_id": bundle,
                "scale": ratio,
                "raw_nodes": len(nodes),
                "perception_ms": round(perception_ms, 1),
                "perception": "unimplemented (A2)" if not nodes else "ok",
            },
        )

    def _safe_tree(self) -> list:
        """A2 is not built yet. Say so in meta rather than crashing the gate."""
        try:
            return self.adapter.raw_tree()
        except NotOnThisPlatform:
            return []

    async def act(self, actions: list[Action]) -> None:
        obs_elements = []  # A2 will pass the live element list through
        self.executor.run(
            actions,
            obs_elements,
            mode=self.mode,
            allowed_bundles=self.task.apps if self.task else None,
        )

    def evaluate(self) -> float:
        if self.task is None:
            raise RuntimeError("evaluate() before reset() — no task loaded")
        score = self.task.check()
        log.info("env.evaluate", task=self.task.task_id, score=score)
        return score

    def close(self) -> None:
        if self.task is not None:
            self.task.teardown()
