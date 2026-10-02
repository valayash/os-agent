"""Jev — TypeSafe's "System One" model — behind the LLMClient seam (§19).

It does not complete chat. It answers NAMED QUESTIONS: POST /systemone with
{model, state, questions}; each answer comes back as a choice (or a score)
with a probability distribution. Request shape taken from the reference repo
(max1874/jev-computer-use, open_computer_use/model.py) — not from TypeSafe's
own docs, and NOT YET EXERCISED AGAINST THE LIVE API FROM HERE.

How it fits LLMClient.plan without bending the contract:
    text   the request STATE, as JSON (built by agent/choice.py)
    extra  {"questions": {...}} — provider-specific, which is what extra is for
    image  must be empty: Jev takes text only. A sparse screen goes to the
           vision planner instead (run.py), never here with the picture dropped.
    schema JevAnswers — the answers verbatim; agent/choice.py decodes them.

Same accounting as LiteLLMClient: pacing and backoff are PROVIDER WAIT, never
agent latency (§5), and cost comes from config.COST_TABLE.
"""

import json
import os
import time

import httpx
import structlog
from pydantic import BaseModel

from os_agent.config import settings
from os_agent.llm.base import LLMResult
from os_agent.llm.litellm_client import BASE_BACKOFF_S, MAX_ATTEMPTS, RateLimiter
from os_agent.telemetry.tracer import price

log = structlog.get_logger(__name__)

RETRY_STATUS = {408, 429, 500, 502, 503, 529}


class JevAnswers(BaseModel):
    """The response, verbatim. Decoding into an action is agent/choice.py's job."""

    answers: dict
    model: str = ""
    usage: dict = {}


class JevError(RuntimeError):
    """The request failed. Nothing executed — a decision request changes nothing."""


def _usage(u: dict) -> tuple[int, int]:
    """Token counts under whichever names the response uses. Unknown = 0."""
    p = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    c = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    return int(p), int(c)


class JevClient:
    """Implements the LLMClient protocol (CLAUDE.md §7)."""

    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None,
                 max_attempts: int = MAX_ATTEMPTS, rpm: int | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.model = model  # "jev/jev-latest" — the COST_TABLE key
        self.provider = "jev"
        self.remote_model = model.split("/", 1)[1] if "/" in model else model
        self.base_url = (base_url or settings.jev_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("JEV_API_KEY", "")
        self.max_attempts = max_attempts
        self.limiter = RateLimiter(settings.llm_rpm if rpm is None else rpm)
        self.http = httpx.Client(timeout=60, transport=transport)

    def plan(self, *, system: str, text: str, image_png: bytes,
             schema: type[BaseModel], extra: dict) -> LLMResult:
        if image_png:
            raise JevError("Jev takes text only; route an image to the vision planner")
        if "questions" not in extra:
            raise JevError("Jev needs extra['questions']; build them with agent/choice.py")
        body = {"model": self.remote_model, "state": json.loads(text),
                "questions": extra["questions"]}
        _ = system  # the instructions travel inside each question

        wait_ms = self.limiter.wait()
        for attempt in range(1, self.max_attempts + 1):
            t0 = time.perf_counter()
            try:
                resp = self.http.post(f"{self.base_url}/systemone", json=body,
                                      headers={"Authorization": f"Bearer {self.api_key}"})
            except httpx.HTTPError as exc:
                failed = (time.perf_counter() - t0) * 1000
                if attempt == self.max_attempts:
                    raise JevError(f"could not reach {httpx.URL(self.base_url).host}: "
                                   f"{type(exc).__name__}: {exc}") from exc
                wait_ms += failed + self._backoff(attempt, type(exc).__name__)
                continue
            ms = (time.perf_counter() - t0) * 1000
            if resp.status_code in RETRY_STATUS and attempt < self.max_attempts:
                wait_ms += ms + self._backoff(attempt, f"HTTP {resp.status_code}")
                continue
            if resp.is_error:
                # The key is in a header, never in this message.
                raise JevError(f"Jev returned HTTP {resp.status_code}: {resp.text[:300]}")

            payload = resp.json()
            parsed = schema.model_validate(payload)
            p, c = _usage(parsed.usage)
            return LLMResult(
                parsed=parsed, prompt_tokens=p, completion_tokens=c,
                latency_ms=ms, provider_wait_ms=wait_ms,
                cost_usd=price(self.model, p, c), repairs=0,
                model=self.model, provider=self.provider,
                raw={"attempts": attempt},
            )
        raise JevError("unreachable: the loop returns or raises")

    def _backoff(self, attempt: int, why: str) -> float:
        backoff = BASE_BACKOFF_S * (2 ** (attempt - 1))
        log.warning("jev.retry", attempt=attempt, error=why, backoff_s=backoff)
        time.sleep(backoff)
        return backoff * 1000
