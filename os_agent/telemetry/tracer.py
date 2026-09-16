"""Per-call instrumentation (CLAUDE.md §8.9).

Every model call is recorded. No exceptions — §5 makes this a hard constraint,
because Phase B compares runs against a baseline, and a baseline you did not
instrument is a baseline you cannot compare to.

THE THREE-WAY LATENCY SPLIT IS THE POINT.

A4 measured five real calls carrying 111,520 ms of free-tier queueing against
21,800 ms of actual work. Reporting wall-clock would have overstated our own
latency by 6x while telling you nothing about the agent. So:

    wall_clock  = everything that elapsed
    provider_wait = 429/503 backoff and queueing   <- THEIRS
    agent_latency = wall_clock - provider_wait     <- OURS, the reported number
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import structlog

from os_agent.config import cost_usd

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CallRecord:
    node: str  # which graph node made the call
    step: int
    model: str
    provider: str
    latency_ms: float  # ours
    provider_wait_ms: float  # theirs
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    repairs: int  # schema repair retries used (0 or 1)
    attempts: int  # how many times we had to ask before one landed
    ok: bool
    error: str = ""


@dataclass
class Tracer:
    """Accumulates every call in a run. One tracer per task."""

    records: list[CallRecord] = field(default_factory=list)

    def record(self, rec: CallRecord) -> None:
        self.records.append(rec)
        log.info(
            "llm.call",
            node=rec.node,
            step=rec.step,
            model=rec.model,
            latency_ms=round(rec.latency_ms),
            provider_wait_ms=round(rec.provider_wait_ms),
            tokens=f"{rec.prompt_tokens}+{rec.completion_tokens}",
            cost_usd=round(rec.cost_usd, 5),
            attempts=rec.attempts,
            repairs=rec.repairs,
            ok=rec.ok,
        )

    # -- aggregates, straight into RunResult (§8.10) ------------------------
    @property
    def calls(self) -> int:
        return len(self.records)

    @property
    def cost(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def provider_wait_s(self) -> float:
        return sum(r.provider_wait_ms for r in self.records) / 1000

    @property
    def agent_latency_s(self) -> float:
        return sum(r.latency_ms for r in self.records) / 1000

    @property
    def prompt_tokens(self) -> int:
        return sum(r.prompt_tokens for r in self.records)

    @property
    def completion_tokens(self) -> int:
        return sum(r.completion_tokens for r in self.records)

    @property
    def repair_rate(self) -> float:
        """Each repair is an EXTRA call — the thing §1 exists to minimise."""
        return (sum(r.repairs for r in self.records) / self.calls) if self.calls else 0.0

    @property
    def per_call_latency_ms(self) -> list[float]:
        """Feeds the latency-vs-step curve — the chart the project exists for."""
        return [r.latency_ms for r in self.records]

    def summary(self) -> dict:
        return {
            "llm_calls": self.calls,
            "agent_latency_s": round(self.agent_latency_s, 2),
            "provider_wait_s": round(self.provider_wait_s, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost, 5),
            "schema_repair_rate": round(self.repair_rate, 3),
        }


@contextmanager
def timed():
    """Wall-clock for a block, in ms."""
    t0 = time.perf_counter()
    box = {}
    try:
        yield box
    finally:
        box["ms"] = (time.perf_counter() - t0) * 1000


def price(model: str, prompt: int, completion: int) -> float:
    """Our table, never the library's guess (§8.1)."""
    return cost_usd(model, prompt, completion)
