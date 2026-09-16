"""Settings and the cost table.

Every model name, budget and backend choice enters the program here and
nowhere else (CLAUDE.md §4.8, §11). If you find yourself typing a model
string anywhere else in the codebase, that is the bug.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------
# Cost table — $ per 1M tokens, (input, output)
# --------------------------------------------------------------------------
# Ours, not the LLM library's. Provider tables lag new models, and the tracer
# needs a number it can trust (CLAUDE.md §8.1).
#
# !! Gemini 3.x Flash pricing below is INTRODUCTORY and EXPIRES 2026-12-31,
# !! after which it becomes $1.50 / $7.50. Re-check this table in January.
COST_TABLE: dict[str, tuple[float, float]] = {
    "gemini/gemini-3.8-flash": (0.75, 3.75),
    "gemini/gemini-3.7-flash": (0.75, 3.75),
    "gemini/gemini-3.6-flash": (0.75, 3.75),
    "gemini/gemini-3.5-flash": (1.50, 9.00),
    "gemini/gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini/gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini/gemini-3.1-pro-preview": (2.00, 12.00),
    "anthropic/claude-opus-5": (5.00, 25.00),
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
}


class UnknownModelCost(KeyError):
    """Raised rather than guessing. A wrong cost is worse than no cost."""


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Price one call. Unknown models raise — silence would corrupt the budget."""
    try:
        cin, cout = COST_TABLE[model]
    except KeyError as exc:
        raise UnknownModelCost(
            f"{model!r} is not in COST_TABLE. Add it (with a source) before running — "
            "an unpriced call breaks budget enforcement and the ablation table."
        ) from exc
    return (prompt_tokens * cin + completion_tokens * cout) / 1_000_000


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_f(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _env_i(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


@dataclass(frozen=True)
class Settings:
    # models
    planner: str = field(default_factory=lambda: _env("MODEL_PLANNER", "gemini/gemini-3.8-flash"))
    small: str = field(default_factory=lambda: _env("MODEL_SMALL", "gemini/gemini-3.1-flash-lite"))
    judge: str = field(default_factory=lambda: _env("MODEL_JUDGE", "gemini/gemini-3.1-pro-preview"))

    # provider knobs -> LLMClient.extra (CLAUDE.md §8.1)
    thinking_level: str = field(default_factory=lambda: _env("THINKING_LEVEL", "low"))
    media_resolution: str = field(default_factory=lambda: _env("MEDIA_RESOLUTION", "medium"))
    llm_rpm: int = field(default_factory=lambda: _env_i("LLM_RPM", 10))

    # limits
    max_steps: int = field(default_factory=lambda: _env_i("MAX_STEPS", 50))
    task_budget_usd: float = field(default_factory=lambda: _env_f("TASK_BUDGET_USD", 0.50))
    suite_budget_usd: float = field(default_factory=lambda: _env_f("SUITE_BUDGET_USD", 5.00))

    # backends
    backend: str = field(default_factory=lambda: _env("BACKEND", "desktop"))
    platform: str = field(default_factory=lambda: _env("PLATFORM", "macos"))
    approval: bool = field(default_factory=lambda: _env("APPROVAL", "on").lower() != "off")

    @property
    def sandbox(self) -> Path:
        """Every file the agent touches lives under here (CLAUDE.md §8.6)."""
        return Path(_env("SANDBOX_DIR", "~/os-agent-sandbox")).expanduser()

    def planner_extra(self) -> dict:
        """Provider-specific knobs. Passed through LLMClient, never abstracted.

        MEASURED A4: Gemini rejects the friendly value. `media_resolution` wants
        the protobuf enum spelling — "MEDIA_RESOLUTION_MEDIUM", not "medium":

            400 Invalid value at 'generation_config.media_resolution' "medium"

        We keep the friendly words in .env because the file is read by humans,
        and translate here. The knob NAME was right; only the spelling was not,
        which is a good argument for sending one real request before building
        five files on top of an assumption.
        """
        if self.planner.startswith("gemini/"):
            return {
                "thinking_level": self.thinking_level,
                "media_resolution": f"MEDIA_RESOLUTION_{self.media_resolution.upper()}",
            }
        if self.planner.startswith("anthropic/"):
            return {"effort": self.thinking_level}
        return {}


settings = Settings()
