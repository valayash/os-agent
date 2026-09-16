"""The default LLMClient — one call signature over ~100 providers (§8.1).

RETRY IS MANDATORY, NOT DEFENSIVE.
A4 measured the free tier returning 503 on most calls: 15 attempts were needed
to land 5 successes, and one call failed outright after 5 backed-off retries.
An agent without backoff here does not work at all on a free key.

Every millisecond spent backing off is PROVIDER WAIT, never agent latency.
"""

import base64
import time

import litellm
import structlog
from pydantic import BaseModel

from os_agent.config import settings
from os_agent.llm.base import LLMResult
from os_agent.llm.schema import REPAIR_INSTRUCTION, SchemaFailure, parse_or_none
from os_agent.telemetry.tracer import price

log = structlog.get_logger(__name__)

litellm.drop_params = True  # a provider that lacks a knob ignores it, not errors

# Retry these. They mean "come back later", not "you asked wrong".
RETRYABLE = ("RateLimitError", "ServiceUnavailableError", "InternalServerError",
             "APIConnectionError", "Timeout", "APIError")
MAX_ATTEMPTS = 5
BASE_BACKOFF_S = 1.0


class RateLimiter:
    """Minimum interval between calls (CLAUDE.md §8.1, LLM_RPM).

    Pacing is cheaper than backoff. A4 measured the free tier returning 503 on
    most calls when hammered; spacing requests out avoids the rejection instead
    of recovering from it.

    Time spent pacing counts as PROVIDER WAIT, not agent latency. It exists
    only because of the provider's limits, so attributing it to our agent would
    make our own number worse for someone else's constraint (§5).
    """

    def __init__(self, rpm: int) -> None:
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last = 0.0

    def wait(self) -> float:
        """Blocks if needed. Returns the milliseconds spent waiting."""
        if self.min_interval <= 0:
            return 0.0
        elapsed = time.monotonic() - self._last
        sleep_for = self.min_interval - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)
            self._last = time.monotonic()
            return sleep_for * 1000
        self._last = time.monotonic()
        return 0.0


class LiteLLMClient:
    """Implements the LLMClient protocol (CLAUDE.md §7)."""

    def __init__(
        self, model: str, *, max_attempts: int = MAX_ATTEMPTS, rpm: int | None = None
    ) -> None:
        self.model = model
        self.provider = model.split("/", 1)[0] if "/" in model else "unknown"
        self.max_attempts = max_attempts
        self.limiter = RateLimiter(settings.llm_rpm if rpm is None else rpm)

    def plan(
        self,
        *,
        system: str,
        text: str,
        image_png: bytes,
        schema: type[BaseModel],
        extra: dict,
    ) -> LLMResult:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._content(text, image_png)},
        ]
        raw, usage, latency_ms, wait_ms, attempts = self._send(messages, schema, extra)

        parsed = parse_or_none(raw, schema)
        repairs = 0
        if parsed is None:
            # ONE repair. Never two (§11) — a repair is an extra billed call.
            repairs = 1
            log.warning("llm.schema_repair", model=self.model, raw=raw[:200])
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": REPAIR_INSTRUCTION})
            raw2, usage2, ms2, wait2, att2 = self._send(messages, schema, extra)
            latency_ms += ms2
            wait_ms += wait2
            attempts += att2
            usage = (usage[0] + usage2[0], usage[1] + usage2[1])
            parsed = parse_or_none(raw2, schema)
            if parsed is None:
                raise SchemaFailure(
                    f"{self.model} produced unparseable output twice. Last reply: {raw2[:300]}"
                )

        return LLMResult(
            parsed=parsed,
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            latency_ms=latency_ms,
            provider_wait_ms=wait_ms,
            cost_usd=price(self.model, usage[0], usage[1]),
            repairs=repairs,
            model=self.model,
            provider=self.provider,
            raw={"attempts": attempts},
        )

    # ----------------------------------------------------------------------
    @staticmethod
    def _content(text: str, image_png: bytes) -> list[dict]:
        """LiteLLM normalises this shape to each provider's own encoding."""
        b64 = base64.b64encode(image_png).decode()
        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]

    def _send(self, messages, schema, extra) -> tuple[str, tuple[int, int], float, float, int]:
        """Returns (raw, (prompt, completion), agent_ms, provider_wait_ms, attempts)."""
        wait_ms = self.limiter.wait()  # pacing is the provider's time, not ours
        last: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            t0 = time.perf_counter()
            try:
                resp = litellm.completion(
                    model=self.model,
                    messages=messages,
                    response_format=schema,
                    **extra,
                )
                ms = (time.perf_counter() - t0) * 1000
                u = resp.usage
                return (resp.choices[0].message.content,
                        (u.prompt_tokens, u.completion_tokens), ms, wait_ms, attempt)
            except Exception as exc:  # noqa: BLE001
                failed_ms = (time.perf_counter() - t0) * 1000
                name = type(exc).__name__
                if not any(r in name for r in RETRYABLE) or attempt == self.max_attempts:
                    raise
                backoff = BASE_BACKOFF_S * (2 ** (attempt - 1))
                # BOTH the failed attempt and the sleep are the provider's time.
                wait_ms += failed_ms + backoff * 1000
                log.warning("llm.retry", attempt=attempt, error=name,
                            backoff_s=backoff, waited_ms=round(wait_ms))
                last = exc
                time.sleep(backoff)

        raise last  # unreachable; the loop raises first
