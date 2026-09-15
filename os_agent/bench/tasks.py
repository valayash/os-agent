"""The task suite.

There is no VM snapshot on a live desktop, so determinism comes from the
sandbox: every task tears down, rebuilds ~/os-agent-sandbox/, and sets itself
up from scratch (CLAUDE.md §8.11).

Checkers inspect END STATE ONLY. A checker that reads the agent's action trace
is grading its own homework (CLAUDE.md §2).

TASKS FREEZE AT THE A6 GATE. After the baseline is recorded, a task's setup,
goal and check are immutable.
"""

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from os_agent.config import settings

TEXTEDIT = "com.apple.TextEdit"
FINDER = "com.apple.finder"


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    goal: str  # the instruction handed to the agent, verbatim
    apps: list[str]  # bundle ids the agent may touch — the policy allowlist
    setup: Callable[[], None]
    check: Callable[[], float]  # END STATE only -> 0.0 | 1.0
    teardown: Callable[[], None]
    difficulty: str = "easy"
    tags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# shared helpers — trusted project code, NOT the agent's action space.
# subprocess is allowed here and nowhere the planner can reach (§8.6).
# --------------------------------------------------------------------------
def fresh_sandbox() -> Path:
    box = settings.sandbox
    if box.exists():
        shutil.rmtree(box)
    box.mkdir(parents=True)
    return box


def quit_app(name: str) -> None:
    subprocess.run(["pkill", "-x", name], capture_output=True, check=False)


def open_with(app: str, path: Path) -> None:
    subprocess.run(["open", "-a", app, str(path)], check=True)


# --------------------------------------------------------------------------
# textedit_append — the A1 gate task
# --------------------------------------------------------------------------
SEED_LINES = ["alpha", "beta", "gamma"]


def _te_setup() -> None:
    box = fresh_sandbox()
    (box / "notes.txt").write_text("\n".join(SEED_LINES) + "\n", encoding="utf-8")
    open_with("TextEdit", box / "notes.txt")


def _te_check() -> float:
    """Did the last line become 'Reviewed', with the seed content intact?"""
    f = settings.sandbox / "notes.txt"
    if not f.exists():
        return 0.0
    lines = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) != len(SEED_LINES) + 1:
        return 0.0
    if lines[: len(SEED_LINES)] != SEED_LINES:
        return 0.0
    return 1.0 if lines[-1] == "Reviewed" else 0.0


def _te_teardown() -> None:
    quit_app("TextEdit")
    if settings.sandbox.exists():
        shutil.rmtree(settings.sandbox)


TEXTEDIT_APPEND = TaskSpec(
    task_id="textedit_append",
    goal="Add a line saying Reviewed at the end of the document, then save it.",
    apps=[TEXTEDIT],
    setup=_te_setup,
    check=_te_check,
    teardown=_te_teardown,
    difficulty="easy",
    tags=["text", "save"],
)

REGISTRY: dict[str, TaskSpec] = {t.task_id: t for t in (TEXTEDIT_APPEND,)}


def get(task_id: str) -> TaskSpec:
    try:
        return REGISTRY[task_id]
    except KeyError as exc:
        raise KeyError(f"unknown task {task_id!r}; have {sorted(REGISTRY)}") from exc
