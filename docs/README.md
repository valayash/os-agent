# docs/

| File | What it is |
|---|---|
| `inside-os-agent.html` | System design explainer — the loop, the three seams, grounding, the perception pipeline, the step budget, and the phase gates. Open it in a browser; no build step, no dependencies. |

## Out of date

The page was written at phase A2 and has not been updated since. It still shows A2 as
the current phase and the model call as an estimated ~1,500 ms. The measured figure is
4,365 ms on the free tier (CLAUDE.md §12), and phases A3–A5 have since passed. It does
not cover the accessibility execution path or the Jev backend. CLAUDE.md is the current
reference.

## Colour rule

The page colour-codes its own figures: **teal = measured on the dev machine, amber =
estimate.** When a figure is measured, update both the spec line in `CLAUDE.md` and the
figure on the page, and flip it from amber to teal.
