# docs/

| File | What it is |
|---|---|
| `inside-os-agent.html` | System design explainer — the loop, the three seams, grounding, the perception pipeline, the step budget, and the gate progression. Open it in a browser; no build step, no dependencies. |

## Keeping it honest

The page colour-codes its own figures: **teal = measured on this machine, amber = still an
estimate.** That is not decoration — it is the same rule as CLAUDE.md §5, where `MEASURED`
notes replace guesses in the spec. When a gate produces a real number, update BOTH:

1. the spec line in `CLAUDE.md`
2. the corresponding figure here, and flip it from amber to teal

The step-budget bar is mostly amber today because no model call has been made yet. A4 is
what repaints it.
