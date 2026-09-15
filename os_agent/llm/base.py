"""SEAM #2 — the model.

Every model call in the project goes through this protocol (CLAUDE.md §4.9,
§11). Swapping Gemini for Anthropic for a local model is an env var, and the
cost/latency instrumentation lives in exactly one place.

No implementation here — litellm_client.py arrives at A4.
"""

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel


@dataclass(slots=True)
class LLMResult:
    """One model call, fully accounted for.

    The two latency fields are the point. Provider queueing is not our agent's
    latency, and reporting it as such would corrupt the one number this whole
    project exists to produce (CLAUDE.md §5, §8.10).
    """

    parsed: BaseModel
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float  # our measurement of the call itself
    provider_wait_ms: float  # 429 backoff / queueing — EXCLUDED from headline
    cost_usd: float  # from config.COST_TABLE, never the library's guess
    repairs: int  # schema repair retries used. 0 or 1, never more (§11)
    model: str
    provider: str  # a slow provider is not a slow model — record which
    raw: dict = field(default_factory=dict)


class LLMClient(Protocol):
    def plan(
        self,
        *,
        system: str,
        text: str,
        image_png: bytes,
        schema: type[BaseModel],
        extra: dict,  # provider knobs: thinking_level, media_resolution, effort
    ) -> LLMResult: ...
