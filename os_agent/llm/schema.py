"""Structured output, and exactly one repair attempt (CLAUDE.md §8.1, §11).

A4 measured the important fact here: Gemini 3.x ACCEPTS our schema as-is.
The `coords` field generates JSON Schema `prefixItems`, which was flagged as a
risk at A1 and deliberately left unfixed rather than guessed at. It works. Had
we "fixed" it pre-emptively we would have changed working code for no reason.

THE REPAIR BUDGET IS ONE.
A repair is a second model call for a single step, which is precisely the cost
§1 exists to avoid. If `schema_repair_rate` is not near zero, the schema is
wrong for the provider and we fix the schema — never the retry budget.
"""

import json

from pydantic import BaseModel, ValidationError

REPAIR_INSTRUCTION = (
    "Your previous reply did not match the required schema. "
    "Reply again with ONLY valid JSON matching the schema exactly. "
    "No prose, no markdown fences, no explanation."
)


class SchemaFailure(RuntimeError):
    """Two attempts, still unparseable. The step fails rather than guessing."""


def strip_fences(text: str) -> str:
    """Some providers wrap JSON in markdown even when told not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


def parse(raw: str, schema: type[BaseModel]) -> BaseModel:
    """Parse, or raise. No silent coercion — a wrong action is worse than none."""
    return schema.model_validate_json(strip_fences(raw))


def parse_or_none(raw: str, schema: type[BaseModel]) -> BaseModel | None:
    try:
        return parse(raw, schema)
    except (ValidationError, json.JSONDecodeError, ValueError):
        return None
