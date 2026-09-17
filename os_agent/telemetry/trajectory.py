"""Write every run to disk (CLAUDE.md §4.6, §10).

Not a log. A record complete enough to REPLAY, because Phase B's trajectory
cache, RAG retrieval and speculation predictor all train on these files. An
optimization you cannot evaluate offline has to be evaluated by spending money
on live runs, which is slow and noisy.

Two things §4.7 insists on and this file honours:

  * every step records (role, name, bbox) ALONGSIDE element_id. IDs are
    positional and renumber every step, so a bare "click element 12" is
    meaningless on replay.
  * screenshots are referenced, not inlined. A 50-step run at ~90 KB a frame is
    4.5 MB of base64 in a JSON file nobody can read.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from os_agent.types import Action, Element, Expectation

log = structlog.get_logger(__name__)
RUNS = Path("runs")


@dataclass
class StepRecord:
    step: int
    app: str
    bundle_id: str
    subgoal: str
    reasoning: str
    action: dict | None
    # §4.7 — the stable key, so this step is replayable after ids renumber
    action_target: dict | None
    expect: dict | None
    outcome: str
    reason: str
    elements: int
    perception_ms: float
    llm_latency_ms: float
    provider_wait_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    repairs: int
    facts: list[str] = field(default_factory=list)
    reflection_note: str = ""
    screenshot: str = ""  # a path, never the bytes


def target_of(action: Action | None, elements: list[Element]) -> dict | None:
    """Resolve element_id to something that survives renumbering."""
    if action is None or action.element_id is None:
        return None
    for e in elements:
        if e.id == action.element_id:
            return {"id": e.id, "role": e.role, "name": e.name, "bbox": list(e.bbox)}
    return {"id": action.element_id, "role": "?", "name": "?", "bbox": None}


def _plain(obj) -> dict | None:
    if obj is None:
        return None
    if isinstance(obj, Action | Expectation):
        return obj.model_dump() if hasattr(obj, "model_dump") else asdict(obj)
    return asdict(obj)


class TrajectoryWriter:
    """One directory per run: runs/<timestamp>/<task_id>/."""

    def __init__(self, task_id: str, goal: str, model: str, provider: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.dir = RUNS / stamp / task_id
        self.frames = self.dir / "frames"
        self.frames.mkdir(parents=True, exist_ok=True)
        self.meta = {
            "task_id": task_id, "goal": goal, "model": model, "provider": provider,
            "started_at": stamp,
        }
        self.steps: list[StepRecord] = []
        # Written up front so a killed run still says what it was trying.
        (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2))

    def add(self, rec: StepRecord, screenshot_png: bytes | None = None) -> None:
        """Record one step — AND FLUSH IT TO DISK IMMEDIATELY.

        Steps used to live in memory until close(), so Ctrl+C threw the entire
        run away. That is backwards: the runs you most need to read are
        exactly the ones you had to kill. It cost us a real diagnosis — a
        killed WhatsApp run left an EMPTY directory, so the question "what did
        the model actually type?" had to be answered by arithmetic on the
        console log instead of by reading the field.

        JSONL, appended a line at a time, because a partial run must still be
        a readable file. trajectory.json is still written by close() for a run
        that finishes normally; steps.jsonl is what survives a kill.
        """
        if screenshot_png:
            path = self.frames / f"{rec.step:03d}.png"
            path.write_bytes(screenshot_png)
            rec.screenshot = str(path.relative_to(self.dir))
        self.steps.append(rec)
        try:
            with (self.dir / "steps.jsonl").open("a") as fh:
                fh.write(json.dumps(asdict(rec), default=str) + "\n")
        except OSError as exc:  # never let bookkeeping kill a run
            log.warning("trajectory.flush_failed", error=str(exc)[:120])

    def close(self, result: dict) -> Path:
        out = self.dir / "trajectory.json"
        out.write_text(json.dumps(
            {"meta": self.meta, "result": result,
             "steps": [asdict(s) for s in self.steps]},
            indent=2, default=str,
        ))
        log.info("trajectory.written", path=str(out), steps=len(self.steps))
        return out
