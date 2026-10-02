# os-agent

A computer-use agent for macOS. It reads the screen through the accessibility tree and a
screenshot, asks a model for one action per step, does it with real mouse and keyboard
input, and then checks the result without a second model call.

Provider-agnostic across any vision LLM. Platform-agnostic by design; **macOS
implemented**, Windows and Linux adapters unimplemented.

## Status

Build phases A0–A5 have passed. There are **no benchmark results yet**: the suite has
one task (`textedit_append`), and the harness that runs a full suite is not built.

| Phase | What passed |
|---|---|
| A0 | Screen Recording + Accessibility granted; capture:point ratio measured (2.0 on the dev machine) |
| A1 | TextEdit task driven end to end through the `Environment` interface, with policy enforced |
| A2 | Accessibility tree → numbered elements → annotated screenshot, checked by eye on 7 apps |
| A3 | LangGraph loop on a scripted replay backend: 7/7 exit paths correct |
| A4 | `textedit_append` completed against a real model: 5 steps, 5 calls (1.00 per step), checker 1.0 |
| A5 | Reflection when stuck, task budget, request pacing, trajectories on disk: 6/6 |

## How a step works

```
observe → plan → execute → verify ─┬─ success ──→ observe
                                   ├─ done ─────→ end
                                   ├─ step cap ─→ end (fail)
                                   └─ stuck ────→ reflect → plan, or end (fail)
```

- **observe** — screenshot (downscaled to points) and accessibility-tree walk, run
  concurrently, turned into a numbered element list and an annotated image.
- **plan** — one model call. The model returns the action, a machine-checkable
  expectation, and a confidence. The prompt is rebuilt each step; there is no message
  history.
- **execute** — the action goes through `actions/policy.py` (app allowlist, approval for
  irreversible targets and dangerous key chords, a cap on repeated sends) and then to the
  OS.
- **verify** — checks the expectation against the new screen: `success`, `no_change`,
  `error`, or `ambiguous`. This capture becomes the next step's observation.
- **reflect** — one extra model call after repeated failures or a detected loop, giving a
  one-line correction.

## Layout

```
os_agent/
  types.py          Element, Observation, Action, PlannedAction, Expectation, ...
  config.py         every model name, price, budget and switch (read from .env)
  llm/              the model seam: LiteLLM client, Jev client, schema repair
  env/              the environment seam: DesktopEnv (real machine), ReplayEnv (tests)
  desktop/          the OS seam: macos.py implemented; windows.py, linux.py raise
  perception/       raw nodes → numbered elements; screenshot annotation
  actions/          executor and guardrails
  verify/cheap.py   verification without a model call
  agent/            graph, nodes, state, prompts, planners, Jev choice questions
  telemetry/        per-call latency/token/cost tracing; trajectory files
  bench/tasks.py    task definitions (currently: textedit_append)
  run.py            command line
scripts/            permission/gate checks and diagnostics — see scripts/README.md
tests/              unit tests; run without a Mac except the one capture test
docs/               design explainer page — see docs/README.md
```

## Setup

Python 3.11, macOS.

```bash
uv venv -p 3.11 && source .venv/bin/activate
uv pip install -e '.[dev]'
cp .env.example .env       # add an API key and pick MODEL_PLANNER
python scripts/a0_gate.py  # checks the macOS permissions and the capture scale
```

Accessibility and Screen Recording must be granted to the app that launches Python —
see `scripts/README.md`.

## Running

```bash
# the one task, asking before every action (the default)
python -m os_agent.run --task textedit_append

# without routine approval prompts — irreversible actions still ask
python -m os_agent.run --task textedit_append --yolo

# any instruction, no checker; --app focuses and fits that app first
python -m os_agent.run --freeform "make the title bold" --app TextEdit
```

Each run prints steps, LLM calls per step, agent latency (excluding provider queueing),
tokens and cost, and writes a trajectory to `runs/<timestamp>/<task_id>/`.

A person must be at the keyboard: macOS ignores programmatic app switching during
unattended automation, so the target app has to be in front. Apps must share one Space,
with no full-screen apps and Stage Manager off.

## Configuration

All switches live in `.env` (see `.env.example`). The ones that change behaviour:

| Variable | Default | Effect |
|---|---|---|
| `MODEL_PLANNER` | `gemini/gemini-3.8-flash` | the model making each decision; any LiteLLM model id, or `jev/jev-latest` |
| `MODEL_SMALL` | `gemini/gemini-3.1-flash-lite` | used by the Jev backend for typed text, reflection and sparse screens |
| `THINKING_LEVEL`, `MEDIA_RESOLUTION` | `low`, `medium` | passed through to Gemini |
| `LLM_RPM` | `10` | request pacing |
| `MAX_STEPS`, `TASK_BUDGET_USD` | `50`, `0.50` | hard limits per run |
| `APPROVAL` | `on` | `off` is the same as `--yolo` |
| `EXEC_MODE` | `synthetic` | `ax` presses and writes through the accessibility API instead of mouse/keyboard |
| `MENU_ACTIONS` | `off` | `on` offers menu-bar commands as `[mN]` items (needs `EXEC_MODE=ax`) |
| `RISK_APPROVAL` | `high` | the model's risk rating at or above this needs approval; `off` disables |
| `AX_MAX_DEPTH` | `60` | accessibility tree depth limit |

`EXEC_MODE=ax`, `MENU_ACTIONS` and the Jev backend are
implemented and unit-tested, but **have not yet been run on a Mac or against the live
Jev API**. The synthetic path is the one the passed phases above were run on.

### Jev backend

`MODEL_PLANNER=jev/jev-latest` with `JEV_API_KEY` set uses TypeSafe's Jev model, which
picks from offered options instead of writing JSON. It reads text only and writes no
text, so typed values come from `MODEL_SMALL` (a second, counted call on typing steps)
and screens with fewer than 3 elements use the screenshot-based planner on `MODEL_SMALL`.

## Tests

```bash
pytest
python scripts/a3_gate.py   # graph exit paths, no machine needed
python scripts/a5_gate.py   # recovery, budget, pacing, trajectories, no machine needed
```
