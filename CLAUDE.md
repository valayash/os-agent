# CLAUDE.md — `os-agent`

> **A computer-use agent that drives a desktop, benchmarked with programmatic checkers,
> optimized for latency at fixed accuracy. Any vision LLM, any OS — by design.**
>
> Claude Code reads this file automatically as project context.

---

## 1. What this project is

A **computer-use agent (CUA)**: an AI system that operates a real desktop by looking at
screenshots and issuing mouse/keyboard actions. No APIs, no application integration.

It drives **this Mac** today, behind a platform seam that Windows and Linux can fill later.
It is evaluated on a **local task suite** — hand-built tasks against real applications
(TextEdit, Finder, LibreOffice, Preview, System Settings), each scored by a program that
inspects final system state.

**This is NOT an operating system.** The OS is unchanged; we drive it from outside.

### The thesis

Published CUAs are accurate but unusably slow — tens of minutes for tasks humans do in a
few. The causes are structural, not model-quality: planning/reflection/judging LLM calls
dominate latency, later steps take up to 3× longer than early ones due to context
accumulation, and top agents use 1.4–2.7× more steps than a human needs.

**Goal: cut latency and cost 5–10× at no loss of success rate.**

Not competing on accuracy. Competing on efficiency at equal accuracy.

### The number that decides the project

**LLM calls per step.** Not model speed, not prompt size. A step costing one 2.5 s vision
call makes a 15-step task ~40 s of model time. The same step costing four calls
(plan + ground + verify + reflect) makes it ~160 s, before any context growth.

Phase A's design target is **1.0 LLM calls per step**. `llm_calls / steps` is a headline
field in `RunResult` from the first run.

### Two phases

1. **Phase A — make it work.** LangGraph agent, correct, measured, baseline frozen. ← **WE ARE HERE**
2. **Phase B — make it fast.** Optimization levers, each independently ablated.

Do not optimize prematurely. But do not make choices that block Phase B — see §4.

---

## 2. Two deliverables

| Artifact | What it is |
|---|---|
| **The benchmark result** — `os_agent.bench` output, ablation table, plots | The evidence |
| **The demo** — `os_agent.run --freeform` + watch UI, on video | What makes anyone look at the evidence |

**This is not a library.** Nobody will import it. The code is a package for directory
structure only — its readers are people judging whether the numbers are real, and us
reproducing runs.

Consequences: no public API to keep stable, no PyPI, no semantic versioning, no docstrings
written for external consumers, and **no abstraction added for hypothetical users**.

### The three seams

There are exactly **three** abstractions. Each exists because *we* cross that boundary
ourselves, not for hypothetical users. Nothing else gets an interface.

| Seam | Protocol | Why it exists |
|---|---|---|
| `env/base.py` | `Environment` | Graph tests run against a replay backend; Phase B replaces the loop; a benchmark backend may return (§17) |
| `llm/base.py` | `LLMClient` | Model choice is an ablation row (§4.8); provider-neutral by default, provider knobs via passthrough |
| `desktop/base.py` | `DesktopAdapter` | macOS implemented, Windows/Linux stubbed; one `Environment` serves all three |

### Credibility of a self-made benchmark

A benchmark you wrote yourself is worthless if you can edit it after seeing results.
Two rules make it honest, and they are not negotiable:

1. **Tasks freeze at the A6 gate.** Once the baseline row is recorded, a task's `setup`,
   `goal` and `check` are immutable. Tuning the agent is the project. Tuning the benchmark
   is fraud.
2. **Checkers inspect end state only** — never the agent's action trace. A checker that
   reads what the agent did is grading its own homework.

Plus a human reference: **you** perform all 15 tasks manually, recording action count and
wall-clock into `tasks/human_reference.json`. Every result is then reported against human
cost, not just against itself.

### What we claim, and what we do not

Claim only what is measured. The README says:

> *Provider-agnostic across any vision LLM. Platform-agnostic by design; **macOS
> implemented and measured**, Windows and Linux adapters unimplemented.*

Claiming cross-platform without a measured suite on each platform is the same sin as
editing a task after the baseline.

---

## 3. Non-goals

- Not building an operating system, desktop environment, or "AIOS"
- Not training or fine-tuning a vision model
- Not chasing any public accuracy leaderboard
- Not running OSWorld — deferred, not deleted (§17)
- Not shipping Windows/Linux adapters in Phase A — the seam exists, the implementations do not
- Not building a product UI — the watch UI is a debug viewer, nothing more
- No accounts, no persistence beyond `runs/`, no deployment

---

## 4. Decisions that protect Phase B

Free now, expensive later. Make them from the first commit.

1. **`Environment.act()` takes `list[Action]`.** Phase A passes lists of length 1. Phase B action-grouping then needs zero interface change.
2. **State has no accumulating fields.** No `Annotated[list, add_messages]`. Prompt is rebuilt from state each step.
3. **`PlannedAction` carries `confidence: float`.** Unused in Phase A; drives cascading and speculation in Phase B.
4. **Verification returns `success | no_change | error | ambiguous`, not a bool.** `ambiguous_rate` is a Phase B target.
5. **`PlannedAction` carries `expect: Expectation`** — a machine-checkable postcondition emitted by the planner in the same call. This is what keeps verification off the LLM and holds calls-per-step at 1.
6. **Every run writes a full trajectory to disk.** Phase B's trajectory cache, RAG retrieval, and speculation predictor all train on these.
7. **Trajectories record `(role, name, bbox)` alongside `element_id`.** IDs are positional and renumber every step; a recorded "click element 12" is unreplayable without a stable key.
8. **Model names come from config, never hardcoded.** We test 4–5 models.
9. **The model call goes through `LLMClient`, never a provider SDK directly.** LiteLLM is the default implementation; `extra: dict` passes provider-specific knobs straight through. Swapping providers is one env var.
10. **Perception is platform-neutral.** The adapter returns *raw* nodes; filtering, dedup and reading-order numbering live in `perception/elements.py`. A future Windows adapter writes a tree walk, not a second perception stack.

---

## 5. Hard constraints

| Constraint | Reason |
|---|---|
| Environment access ONLY through `Environment` | Phase B swaps the loop; env layer must not move |
| Model access ONLY through `LLMClient` | Provider swap is one env var; instrumentation lives in one place |
| OS primitives ONLY through `DesktopAdapter` | The platform seam is the whole cross-platform story |
| State entering the prompt must be fixed-size | The #1 latency cause in published agents |
| **All coordinates are POINTS, never pixels** | Retina. See §5.1 — this is the day-one bug |
| Every LLM call instrumented (latency, tokens, cost) | No exceptions |
| **Provider wait time excluded from headline latency** | 429 backoff is the provider's queue, not our agent |
| No task run without a programmatic success check | Human judgement is not a metric |
| Max 50 steps per task, hard cap | Runaway loops cost real money |
| Task AND suite cost budgets, enforced in code | Abort when exceeded |
| Agent may only act on apps in `TaskSpec.apps` | It drives the real machine |

### 5.1 The coordinate contract

**Measured at A0 on this machine — three resolutions exist, and only two matter:**

| Surface | Size | Used by |
|---|---|---|
| Physical panel | 2560 × 1600 | **nobody — a red herring** |
| Framebuffer (what capture returns) | 2880 × 1800 | screen capture |
| Logical points | 1440 × 900 | AX, pyautogui, prompts |

**Measured capture:point ratio = exactly 2.0000** (x and y agree). `screencapture` returns
the *framebuffer*, not the panel — macOS renders 2880×1800 and downsamples to the 2560×1600
panel in hardware, so the panel spec never enters our arithmetic. Reading the panel
resolution off `system_profiler` and dividing by it would put every click off by 11%.

| Subsystem | Space |
|---|---|
| screen capture output | **pixels** |
| AX `AXPosition` / `AXSize` | **points** |
| `pyautogui.click()` | **points** |

> **Rule:** `Element.bbox`, `Action.coords`, and every number in every prompt are in
> **points**. The screenshot is downscaled to point resolution immediately after capture,
> before SoM rendering. `Observation.meta["scale"]` records the factor. Pixels exist inside
> exactly one function — the adapter's capture call — and nowhere else.

**Measure the ratio; never assume it — including when it comes out clean.** Here the
measured ratio happens to equal `backingScaleFactor` (2.0), so this display is effectively
in a 2× mode from our side. That is a property of the user's current display setting, not a
law: change Displays → Resolution and the framebuffer changes with it. The adapter's
`capture()` therefore *returns* the measured ratio (`DesktopAdapter.capture -> (png, ratio)`)
and `Observation.meta["scale"]` records it per observation. Nothing hardcodes 2.

A unit test asserts screenshot size equals the screen's point dimensions. If it fails, stop.

### 5.2 Fixed-size vs bounded

> **Fields that enter the prompt must be fixed-size. Bookkeeping fields need only be bounded.**

`facts` (capped 10) enters the prompt — fixed. The loop-detector ring buffer never enters
the prompt — bounded is enough, and it costs zero latency.

---

## 6. Repo structure

```
os-agent/
├── CLAUDE.md                   # this file
├── README.md                   # results + ablation table (written last)
├── pyproject.toml
├── .env.example
│
├── os_agent/
│   ├── types.py                # Element, Observation, Action, PlannedAction, Expectation
│   ├── config.py               # models, COST_TABLE, budgets, backend + provider selection
│   │
│   ├── llm/                    # SEAM #2 — any provider
│   │   ├── base.py             # LLMClient protocol
│   │   ├── litellm_client.py   # default: one call signature over ~100 providers
│   │   └── schema.py           # structured output + single repair retry
│   │
│   ├── env/                    # SEAM #1 — task-level contract
│   │   ├── base.py             # Environment ABC
│   │   ├── desktop_env.py      # ONE Environment, parameterized by a DesktopAdapter
│   │   └── replay_env.py       # scripted/recorded observations; graph tests, no machine
│   │
│   ├── desktop/                # SEAM #3 — OS primitives.  NOT "platform" (shadows stdlib)
│   │   ├── base.py             # DesktopAdapter protocol
│   │   ├── macos.py            # AXUIElement + Quartz + Vision + pyautogui  ← IMPLEMENTED
│   │   ├── windows.py          # UI Automation                        ← NotImplementedError
│   │   └── linux.py            # AT-SPI                               ← NotImplementedError
│   │
│   ├── perception/             # PLATFORM-NEUTRAL: raw nodes → list[Element]
│   │   ├── elements.py         # filter, dedup, reading-order ids
│   │   └── som.py              # numbered-box overlay renderer
│   │
│   ├── actions/
│   │   ├── executor.py         # Action → DesktopAdapter calls (points)
│   │   └── policy.py           # app allowlist + irreversibility tiers  ← A1, not A5
│   │
│   ├── verify/
│   │   ├── cheap.py            # expectation check: pixel diff, tree diff, targeted OCR
│   │   └── llm_judge.py        # only for ambiguous outcomes
│   │
│   ├── agent/
│   │   ├── graph.py            # LangGraph assembly
│   │   ├── nodes.py            # observe / plan / execute / verify / reflect
│   │   ├── state.py            # AgentState TypedDict
│   │   └── prompts.py          # prompt builders (rebuilt, never appended)
│   │
│   ├── memory/                 # Phase B — stubs only in Phase A
│   │   ├── trajectory_store.py
│   │   └── retrieval.py
│   │
│   ├── telemetry/
│   │   ├── tracer.py           # per-call latency/token/cost capture
│   │   └── trajectory.py       # full run serialization
│   │
│   ├── bench/
│   │   ├── harness.py          # task runner, sandbox reset, budget + rpm enforcement
│   │   ├── metrics.py          # RunResult, aggregation
│   │   ├── tasks.py            # the 15 tasks: setup / check / teardown
│   │   └── report.py           # table + plots
│   │
│   ├── run.py                  # CLI: single task / --freeform
│   └── watch/                  # Phase A8 — debug viewer
│       ├── server.py           # FastAPI + websocket
│       └── index.html          # single file, no build step
│
├── docs/
│   ├── inside-os-agent.html    # system design explainer — diagrams, budget, gates
│   └── README.md               # how to keep its figures honest (teal=measured, amber=guess)
│
├── scripts/                    # gate checks + diagnostics; never imported by os_agent/
│   ├── a0_gate.py
│   └── a1_gate.py
│
├── tasks/
│   ├── fixtures/               # seed files copied into the sandbox by setup()
│   └── human_reference.json    # your own step count + wall-clock per task
├── runs/                       # trajectories + results (gitignored)
└── tests/
```

---

## 7. Core contracts

**Do not change these without updating this file.**

```python
# os_agent/types.py
from dataclasses import dataclass, field
from typing import Literal

ActionKind = Literal[
    "click", "double_click", "right_click",
    "type", "key", "scroll", "drag",
    "wait", "done", "fail",
]

@dataclass
class Element:
    id: int                                   # index the model references; POSITIONAL
    role: str                                 # normalized: button, textfield, menuitem...
    name: str                                 # accessible name / visible label
    bbox: tuple[int, int, int, int]           # x0, y0, x1, y1 — POINTS
    enabled: bool = True

@dataclass
class Observation:
    screenshot: bytes                         # PNG, downscaled to POINT resolution
    annotated: bytes                          # PNG with SoM overlay
    elements: list[Element]
    meta: dict = field(default_factory=dict)  # app, bundle_id, window title, scale

@dataclass
class Action:
    kind: ActionKind
    element_id: int | None = None
    text: str | None = None
    keys: list[str] | None = None
    amount: int | None = None                 # scroll
    coords: tuple[int, int] | None = None     # escape hatch — POINTS
```

```python
# LLM boundary — Pydantic, not dataclasses.  Provider-neutral schema.
class Expectation(BaseModel):
    kind: Literal["element_appears", "element_disappears",
                  "text_in_element", "screen_changed", "none"]
    value: str | None = None                  # substring to match
    element_id: int | None = None

class PlannedAction(BaseModel):
    reasoning: str                            # <= 2 sentences, capped
    subgoal: str
    action: Action
    expect: Expectation                       # verification without an LLM
    confidence: float                         # Phase B
    new_fact: str | None = None               # at most ONE per step
```

```python
# os_agent/env/base.py — SEAM #1
class Environment(ABC):
    @abstractmethod
    def reset(self, task_id: str) -> Observation:
        """Teardown, rebuild sandbox, run setup, return first observation."""

    @abstractmethod
    def observe(self) -> Observation:
        """Capture screenshot + elements + annotated overlay."""

    @abstractmethod
    def act(self, actions: list[Action]) -> None:
        """Execute in order. List, not single — protects Phase B."""

    @abstractmethod
    def evaluate(self) -> float:
        """Run the task's success checker. Returns 0.0 or 1.0."""

    @abstractmethod
    def close(self) -> None: ...
```

```python
# os_agent/llm/base.py — SEAM #2
@dataclass
class LLMResult:
    parsed: BaseModel
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float                 # our measurement, not the provider's
    provider_wait_ms: float           # 429 backoff — EXCLUDED from headline latency
    cost_usd: float                   # from config.COST_TABLE, not the library's guess
    repairs: int                      # schema repair retries used (0 or 1)
    model: str
    provider: str                     # record it: a slow provider ≠ a slow model

class LLMClient(Protocol):
    def plan(
        self, *,
        system: str,
        text: str,
        image_png: bytes,
        schema: type[BaseModel],
        extra: dict,                  # provider-specific: thinking_level, media_resolution
    ) -> LLMResult: ...
```

```python
# os_agent/desktop/base.py — SEAM #3
class DesktopAdapter(Protocol):
    def screen_size_points(self) -> tuple[int, int]: ...
    def capture(self) -> tuple[bytes, float]:
        """PNG downscaled to POINT resolution, plus the measured capture:point ratio."""
    def frontmost_app(self) -> tuple[str, str]:
        """(display_name, bundle_id) — bundle_id is the policy allowlist key."""
    def raw_tree(self) -> list[RawNode]:
        """Unfiltered accessibility nodes. Filtering happens in perception/."""
    def click(self, x: int, y: int, kind: str) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press_keys(self, keys: list[str]) -> None: ...
    def scroll(self, x: int, y: int, amount: int) -> None: ...
    def ocr(self, image_png: bytes, bbox: tuple[int, int, int, int]) -> str: ...
```

```python
# os_agent/agent/state.py
class AgentState(TypedDict):
    goal: str
    task_id: str

    # current screen ONLY — never accumulate
    app: str
    elements: list[Element]
    screenshot_b64: str
    obs_fresh: bool                # verify captures; observe becomes a no-op

    subgoal: str
    last_action: Action | None
    last_expect: Expectation | None
    last_outcome: str              # success | no_change | error | ambiguous
    facts: list[str]               # HARD CAP 10, enters the prompt
    step: int
    consecutive_failures: int
    recent_hashes: list[str]       # bounded ring(8), loop detection, NOT in prompt
    status: str                    # running | done | failed
```

> **Critical:** no `messages`, no `history`, no `screenshots` list.
> Prompts are rebuilt from state every step. Never appended.

---

## 8. Module specs

### 8.1 `llm/` — the model seam

`litellm_client.py` is the default `LLMClient`. LiteLLM gives one call signature over ~100
providers and normalizes the three things that actually differ:

| Problem | Reality | Resolution |
|---|---|---|
| Image encoding | Anthropic: base64 blocks + `media_type` · OpenAI: `image_url` data URI · Gemini: `inline_data` | LiteLLM normalizes |
| Structured output | Strict JSON schema / best-effort JSON mode / neither | Native first → **one** repair retry → fail the step. Track `schema_repair_rate` |
| Cost | Provider tables lag new models | `config.COST_TABLE` overrides. Ours wins |

**`extra: dict` is the escape hatch, and it is load-bearing.** The provider knobs are
exactly what the thesis measures, so they must not be abstracted away:

| Provider | Knob | Effect |
|---|---|---|
| Gemini 3.x | `thinking_level: "low" \| "high"` | Thinking tokens bill as output — a cost *and* latency lever |
| Gemini 3.x | `media_resolution` | Image token budget. **The screenshot is ~52% of our prompt** |
| Anthropic | `output_config.effort`, `speed: "fast"` | Same levers, different names |

**Do not use Gemini 2.5.** JSON-Schema structured output is **Gemini 3 series only**, and
`PlannedAction` is the spine of the design.

**Retry and throttle.** A suite run is ~225 calls. `LLM_RPM` paces requests; 429s get
exponential backoff. **Backoff time lands in `provider_wait_ms`, never in latency.**

### 8.2 `desktop/` — the platform seam

The adapter exposes OS primitives and nothing else. No filtering, no numbering, no policy.

**`macos.py` — the only implementation in Phase A**

```
frontmost:  NSWorkspace.frontmostApplication() → pid, bundle_id
tree:       AXUIElementCreateApplication(pid) → AXWindows → focused window
              └─ recurse AXChildren, reading
                 AXRole, AXTitle | AXValue | AXDescription, AXPosition, AXSize, AXEnabled
capture:    CGDisplayCreateImage (in-process, no subprocess, no disk)
            MEASURED A1: 55 ms warm / 189 ms cold, 1440x900 PNG, ~91 KB
            stages: CGDisplayCreateImage 18 · frombuffer 6 · reduce(2) 11 · PNG 13
            — A0 uses the `screencapture` CLI instead: a known-good permission canary
ocr:        DEFERRED — raises. See "OCR is DEFERRED" below.
input:      pyautogui, FAILSAFE on
```

**Apple Vision, not Tesseract.** Built into macOS, runs on the Neural Engine, far better on
UI text, zero non-Python dependencies. The single biggest "because we're on a Mac" win.

**The AX walk is slow.** Every attribute read is an IPC round-trip to the target app. Four
mitigations are part of the design, not optimizations:

1. Frontmost app's focused window only — never the whole desktop
2. Depth cap 20, node cap 300
3. Prune zero-size and offscreen subtrees *before* descending
4. `AXUIElementSetMessagingTimeout(0.5)` so one hung app cannot stall a step

**Platform comparison**, recorded so the stubs are filled with eyes open:

| | Windows | macOS | Linux |
|---|---|---|---|
| API | UI Automation | AXUIElement | AT-SPI |
| Speed | Fast | Slow (IPC) | Medium |
| Permissions | None | TCC, per-binary | None |
| Tree quality | **Best** | Decent | Weakest; Wayland often broken |
| Snapshot reset | Hard | Hard | **Easy (VM)** |

Windows UIA is the easiest and richest of the three. Each platform is still 1–2 weeks of
its own debugging, and each needs **its own task suite and its own baseline** — TextEdit and
Finder do not exist on Windows.

**Known weakness (all platforms):** Electron, canvas and some Java apps expose almost
nothing — confirmed at A0, where Claude Desktop returned 8 nodes, all `AXGroup`/`AXWindow`,
with nothing actionable.

**OCR is DEFERRED, not planned (decided at A2).** It would be redundant twice over: the
planner is a vision model that can already read the screen and fall back to `Action.coords`,
and on native apps the AX tree *is* the text oracle — A1 read TextEdit's whole document out
of `AXValue` instantly. The suite is TextEdit / Finder / Preview / System Settings, all
native AppKit, so the AX-blind case may never arise.

What we build instead is the **trigger**, not the remedy: `perception/elements.py` logs
`perception.sparse` when `len(elements) < 3` on a non-empty screen, and the rate lands in
`RunResult`. If it stays zero across the 15 tasks, OCR was never needed. If it fires, we
build `ocr()` against the real failing app rather than an imagined one.

`DesktopAdapter.ocr()` stays on the protocol and keeps raising — the seam costs nothing.

**Watch LibreOffice.** It draws through its own VCL toolkit rather than AppKit and has the
weakest accessibility support of anything in the suite. If `perception.sparse` ever fires,
that is the likely source.

### 8.3 `perception/elements.py` — platform-neutral

`list[RawNode]` → `list[Element]`. Runs identically on every platform.

Role names are **normalized** here (`AXButton` → `button`, `UIA Button` → `button`) so
prompts do not leak platform vocabulary and a model tuned on one platform transfers.

Filter out: invisible · bbox area < 20 pt² · offscreen · disabled · duplicate bboxes.
Assign sequential `id` in reading order (top-to-bottom, left-to-right) — stable ordering
matters because the model references numbers.

### 8.4 `perception/som.py`

Numbered boxes on the screenshot. Red outline, filled label top-left, white text.
**Operates on the point-resolution image** (§5.1) — never on raw pixels.

Must handle: overlapping elements (offset labels) · labels running off-screen ·
very small elements (label outside the box).

**Acceptance:** dump 10 annotated screenshots across different apps and eyeball them.
If boxes are wrong, nothing downstream can work.

### 8.5 `actions/executor.py`

`Action` → `DesktopAdapter` calls, in points. `element_id` resolves to its bbox centre.
`done` / `fail` never reach the executor — the graph terminates on them.
A list of actions executes in order — Phase B's action grouping, already free.

**MEASURED A1 — pyautogui's keyboard is not usable on macOS, and we already swapped it.**
It sends a modifier key-down and lets the app infer the chord, so `hotkey("command","a")`
arrives as a literal `a`: Select All silently becomes typing a character and `cmd+s`
silently stops saving. Fast typing also drops characters — `"Reviewed"` arrived as `"Rviw"`.
All of it is SILENT: no exception, just wrong input that reads exactly like the model
choosing badly. That is the worst failure mode this project can have, because it would be
attributed to the model for weeks.

`desktop/macos.py` now sends keys via `CGEventCreateKeyboardEvent` with explicit flags, and
types via `CGEventKeyboardSetUnicodeString` (unicode-safe, measured 13.9 ms/char). **The
modifier key-up must carry `flags=0`** — leave the mask set and Command stays latched, so
every subsequent character becomes a menu shortcut and the text never appears at all.
Mouse stays on pyautogui; that half is fine.

### 8.6 `actions/policy.py` — guardrails · **built in A1**

```python
IRREVERSIBLE = {"delete", "remove", "send", "purchase", "erase", "format",
                "shut down", "restart", "empty trash", "move to trash"}

def app_allowed(bundle_id: str, task: TaskSpec) -> bool: ...
def requires_approval(action: Action, elements) -> bool: ...
def is_allowed(action: Action, mode: str) -> bool: ...   # mode: bench | freeform
```

- `act()` refuses if the frontmost app's bundle id is not in `TaskSpec.apps`
- Any action whose target label matches `IRREVERSIBLE` always requires approval
- No shell execution, ever
- All agent file operations live under `~/os-agent-sandbox/`

**Approval is the default.** `--yolo` opts out; there is no `--debug` flag to remember.

### 8.7 `verify/cheap.py`

The planner emitted `expect`. Verification is checking it — a string match, not a model call.

| Check | Cost | Answers |
|---|---|---|
| Pixel diff ratio | ~5 ms | Did anything change at all? |
| Tree diff | ~20 ms | Did the expected element appear/disappear? |
| `AXValue` of the target element | ~20 ms | Does the field contain what we typed? |

Resolution: expectation satisfied → `success` · nothing changed at all → `no_change` ·
error dialog or executor exception → `error` · check undecidable → `ambiguous`.

**Track `ambiguous_rate` as a headline metric** — each one is an LLM call we didn't want.

### 8.8 `agent/graph.py`

```
observe → plan → execute → verify ─┬─ success ──→ observe
                                   ├─ done ─────→ END
                                   ├─ step>=50 ─→ END(fail)
                                   ├─ 2 fails ──→ reflect ─┬→ plan
                                   └─ other ────→ reflect ─┴→ END(fail)
```

**One capture per step.** `verify` performs the post-action capture and writes
`elements` / `screenshot_b64` / `obs_fresh=True` into state; `observe` is a no-op when the
observation is already fresh. Capturing twice per step wastes 150–300 ms for nothing.

**Loop detection.** `consecutive_failures` catches failure loops, not success loops —
an agent oscillating between two screens where every action "succeeds". `recent_hashes`
holds `hash(screen_fingerprint, action)` for the last 8 steps; the same pair 3× forces
`reflect`.

Compile with `SqliteSaver` checkpointer (package: `langgraph-checkpoint-sqlite`).
`interrupt_before=["execute"]` is **on by default**.

**Node contract:** each node returns only the state keys it changes. No in-place mutation.

### 8.9 `telemetry/tracer.py`

Wrap every `LLMClient` call: node name, step, wall-clock ms, provider wait ms, prompt
tokens, completion tokens, repairs, **model and provider**, cost estimate from
`config.COST_TABLE`.

Record the provider, always. A slow provider makes a good model look bad, and without the
field the latency numbers are uninterpretable.

LangSmith via `langsmith`'s `@traceable` — works standalone, no LangChain required.
**On from the first commit** — Phase B needs the trace history, and retrofitting
instrumentation throws it away.

### 8.10 `bench/metrics.py`

```python
@dataclass
class RunResult:
    task_id: str
    success: bool

    # latency — three numbers, because only one of them is ours
    wall_clock_s: float           # total elapsed
    provider_wait_s: float        # 429 backoff + queueing
    agent_latency_s: float        # wall_clock_s - provider_wait_s  ← THE reported number

    steps: int
    llm_calls: int
    llm_calls_per_step: float     # THE number (§1)
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    per_step_latency_ms: list[float]
    perception_ms: list[float]    # the tree walk is slow; keep it visible
    ambiguous_rate: float
    escalation_rate: float
    schema_repair_rate: float     # every repair is an extra LLM call
    steps_vs_human: float         # steps / human_reference[task_id].steps
    model: str
    provider: str
    terminal_reason: str          # done | max_steps | stuck | budget | error
```

Aggregate report must include a **latency-vs-step-number curve** — the chart proving our
fixed-size state works where published agents grow 3×.

### 8.11 `bench/tasks.py` — the suite

```python
@dataclass
class TaskSpec:
    task_id: str
    goal: str                      # the instruction given to the agent
    apps: list[str]                # bundle ids the agent may touch
    setup: Callable[[], None]      # build known state in ~/os-agent-sandbox/
    check: Callable[[], float]     # inspect END STATE only → 0.0 | 1.0
    teardown: Callable[[], None]   # nuke sandbox, quit apps
```

There is no VM snapshot. `reset()` = `teardown()` → recreate `~/os-agent-sandbox/` →
`setup()` → `observe()`. Determinism comes from the sandbox, not from the OS.

Checkers read from three places, and nothing else:

| Source | Example |
|---|---|
| Filesystem | file exists / renamed / contents match |
| Document files | `odfpy` / `openpyxl` / `pypdf` parse the saved file |
| `defaults read <domain>` | a preference actually changed |

**Applications silently rewrite what the agent types.** TextEdit autocapitalized typed
`alpha` to `Alpha` during A1 — substitutions (capitalization, smart quotes, smart dashes)
are on by default and will fail a checker for reasons that look like agent error. Two rules:
seed file content from `setup()` in Python rather than by typing it, and make any checker
that compares typed prose explicit about case and punctuation. A task whose success depends
on an app's substitution settings is not a task, it is a coin flip.

**Use LibreOffice, not Numbers/Pages.** LibreOffice is native on Apple Silicon and saves
`.ods`/`.odt`, which parse cleanly in Python. Apple's iWork saves opaque bundles that are
miserable to check.

15 tasks: 5 easy / 5 medium / 5 hard. **Frozen at the A6 gate** (§2).

### 8.12 `run.py` — the CLI

```bash
# single benchmark task, step-through approval (the default)
python -m os_agent.run --task textedit_append

# no approval prompts — only after A5
python -m os_agent.run --task textedit_append --yolo

# arbitrary instruction, no scoring
python -m os_agent.run --freeform "open the spreadsheet and sum column C"

# full benchmark
python -m os_agent.bench --suite all

# model comparison — one env var, no code change
MODEL_PLANNER=gemini/gemini-3.1-pro-preview python -m os_agent.bench --suite all
```

`--freeform` reuses the identical graph. Only difference: `goal` comes from the argument
and there is no `evaluate()` at the end.

### 8.13 `watch/` — debug viewer (Phase A8, half a day)

FastAPI + one HTML file + websocket. Shows live annotated screenshot, current reasoning,
pending action, approve/reject buttons.

**Not a product UI.** No settings, no history, no accounts. A debug viewer that films well.

---

## 9. Phase A build order

Each phase has a gate. **Do not start the next until the gate passes.**

### A0 — Permissions gate (no project code)

macOS gates accessibility and screen capture per-executable, and grants them to the process
that asks. Until this passes, nothing downstream can work.

1. `screencapture` writes a PNG → **Screen Recording** permission
2. `AXUIElementCreateApplication` returns a non-empty tree → **Accessibility** permission
3. `pyautogui` moves the cursor → Accessibility permission
4. Measured capture↔point ratio (§5.1) — measured, not assumed

**Gate:** all four pass from the project venv (Python 3.11 via `uv`).

### A1 — Types, seams, guardrails
`types.py`, `config.py`, all three `base.py` protocols, `actions/policy.py`,
minimal `desktop/macos.py`, `env/desktop_env.py`.
**Gate:** a hardcoded script opens TextEdit, types a line, saves to the sandbox, and a
checker confirms the file — through the `Environment` interface, with policy enforced.

### A2 — Perception
`desktop/macos.py` tree walk, `perception/elements.py`, `perception/som.py`.
No OCR — deferred, see §8.2.
**Gate:** annotated screenshots correct across 10 apps, verified by eye. Points/pixels
unit test green.

### A3 — Graph skeleton
LangGraph with **stub nodes that only print**, driven by `replay_env`. Verify routing,
cycles, termination, step cap, loop detection — before any model call and without touching
the real machine.
**Gate:** 50 stub steps, correct termination on every exit path.

### A4 — Real nodes
Wire `llm/litellm_client.py`. `plan` requests `PlannedAction` as a JSON schema.
`execute` uses the executor. `verify` checks `expect` with pixel diff only at first.
**Gate:** agent completes one real task, with approval prompts on. `schema_repair_rate`
recorded.

### A5 — Reflect, recovery, budget
Two consecutive failures → reflect with fuller context. Task and suite budgets abort a run.
RPM throttle live.
**Gate:** agent recovers from a deliberately hostile task (a dialog that steals focus
mid-task) and aborts cleanly when the budget is exceeded.

### A6 — Benchmark harness
15 tasks with programmatic checkers. Human reference recorded.
**Gate: baseline row recorded, tasks frozen.** *The most important gate in the project.*

### A7 — Baseline hardening
Run the suite 3× to measure variance, **on a paid key** (§12). A benchmark whose noise
exceeds the effect size you intend to claim cannot support a claim.
**Gate:** per-task success variance and latency variance recorded.

### A8 — Demo surface
`watch/` viewer + record the video.
**Gate:** 40-second screen recording of a freeform task completing.

**Phase A complete.**

---

## 10. Conventions

- Python 3.11 (`uv python install 3.11`) · type hints everywhere · `ruff` + `black`
- **Naming:** repo + distribution `os-agent` (hyphen), importable package `os_agent` (underscore).
  Python identifiers cannot contain hyphens, so `pip install os-agent` would import `os_agent`.
- Dataclasses internally, Pydantic only at the LLM boundary
- `async` for all I/O — screen capture and tree walk run concurrently
- Structured JSON logging, never bare `print`
- **All coordinates in points** (§5.1)
- Every module gets a `tests/` file; perception, executor and codec need real unit tests
- Secrets in `.env`, never committed
- Trajectories to `runs/{timestamp}/{task_id}.json`, gitignored

**Commit discipline:** one phase gate per commit. Message states the gate it closes.

---

## 11. Never do these

| Never | Why |
|---|---|
| Add `Annotated[list, add_messages]` to AgentState | Causes the 3× latency growth we exist to fix |
| Call a provider SDK outside `LLMClient` | Breaks the provider swap and the cost accounting |
| Call an OS API outside `DesktopAdapter` | Breaks the platform seam |
| Call the environment outside `Environment` | Breaks the Phase B swap |
| Put filtering or numbering in a desktop adapter | Every new platform would reimplement perception |
| Mix pixels and points | Every click misses by 11% and it looks almost right |
| Report `wall_clock_s` as the latency number | It contains the provider's 429 queue, not our agent |
| Publish latency measured on a free tier | Free tiers are throttled; you'd be measuring a queue |
| Edit a task after the A6 baseline | That is not benchmarking, it is fraud |
| Let a checker read the agent's action trace | Grading its own homework |
| Report a number without a programmatic checker | Not a metric |
| Claim cross-platform without a measured suite per platform | Same sin as editing a task |
| Add an optimization before its baseline is recorded | Unmeasurable = worthless |
| Retry a failing action more than twice | Infinite loops burn money |
| Retry a schema parse more than once | It is an extra LLM call, which is the thing we minimize |
| Hardcode a model or provider outside `config.py` | We test 4–5 models |
| Let the agent act on an app outside `TaskSpec.apps` | It drives the real machine |
| Write outside `~/os-agent-sandbox/` | Same |
| Run with `--yolo` unattended | Same |
| Build the watch UI before A6 | A week of websockets while the agent still can't click |

---

## 12. Environment variables

```bash
# .env.example

# ---- provider: swap by changing these two lines only ----
LLM_PROVIDER=gemini
GEMINI_API_KEY=

MODEL_PLANNER=gemini/gemini-3.8-flash          # the loop
MODEL_SMALL=gemini/gemini-3.1-flash-lite       # Phase B cascade tier
MODEL_JUDGE=gemini/gemini-3.1-pro-preview      # ambiguous fallback + accuracy row

# ---- provider knobs, passed through LLMClient.extra ----
THINKING_LEVEL=low          # low | high   (Gemini 3.x)
MEDIA_RESOLUTION=medium     # image token budget — ablation knob
LLM_RPM=10                  # request pacing; raise on a paid key

# ---- tracing ----
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=
LANGCHAIN_PROJECT=os-agent

# ---- limits ----
MAX_STEPS=50
TASK_BUDGET_USD=0.50
SUITE_BUDGET_USD=5.00

# ---- backends ----
BACKEND=desktop             # desktop | replay
PLATFORM=macos              # macos | windows | linux   (only macos implemented)
SANDBOX_DIR=~/os-agent-sandbox
APPROVAL=on                 # on | off  (off == --yolo)
```

Prices live in `config.COST_TABLE`, not here — the tracer needs them, and Gemini 3.x Flash
introductory pricing ($0.75/$3.75 per 1M) **expires 2026-12-31**, rising to $1.50/$7.50.
Put that date in a comment next to the table.

**Free tier is for iteration, not for results.** Run A6 and A7 on a paid key (§11).

### Cost reference, measured against our step shape

~3,300 prompt tokens (of which the screenshot is ~1,730) + ~300 completion, per step:

| Config | $/step | $/task (15 steps) | $/suite (15 tasks) |
|---|---|---|---|
| Gemini 3.8 Flash, thinking `low` | ~$0.0036 | ~$0.054 | **~$0.81** |
| Gemini 3.8 Flash, thinking `high` | ~$0.011 | ~$0.17 | ~$2.50 |
| Gemini 3.1 Pro Preview | ~$0.010 | ~$0.15 | ~$2.20 |

A suite run under a dollar is why A7's 3-repeat variance pass is affordable.

---

## 13. Phase B preview — do not build yet

Listed so Phase A decisions stay compatible. Each becomes one ablation row.

| Lever | Expected effect |
|---|---|
| Fixed-size context | Flat latency curve (already designed in) |
| `thinking_level` low vs high | Thinking bills as output — cost and latency |
| `media_resolution` down | Screenshot is ~52% of prompt tokens; trades SoM legibility |
| Adaptive settle wait (poll-until-quiet vs blind sleep) | ~1 s/step, zero accuracy cost |
| Pipeline overlap + prompt caching | 30–50% latency, zero accuracy cost |
| Action grouping | 3–5× fewer LLM calls |
| Model cascading + calibration | Large cost reduction; needs a calibration curve |
| Speculative execution | Fewer calls; cut it if misprediction rate is too high |
| Trajectory RAG | Retrieve similar past runs as few-shot context |
| App knowledge RAG | Index macOS/LibreOffice shortcuts; also grows the cacheable prefix |
| Trajectory cache | Order-of-magnitude on repeat tasks |
| Model swap (Flash / Flash-Lite / Pro, and across providers) | Free — one env var |
| **LangGraph vs custom runtime** | Quantify framework overhead — a resume line in itself |

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **CUA** | Computer-use agent |
| **SoM** | Set-of-Marks — numbered boxes on UI elements so the model picks an index rather than pixel coordinates. Worth ~5–8 points on published benchmarks. |
| **a11y tree** | Accessibility tree: `AXUIElement` (macOS), UI Automation (Windows), AT-SPI (Linux) |
| **Points vs pixels** | Logical units vs physical Retina units. See §5.1. |
| **Grounding** | Mapping a described target to an actual screen location |
| **Ablation** | Running with one component disabled to measure its contribution |
| **Expectation** | A machine-checkable postcondition the planner emits with its action |
| **Provider wait** | Time spent in 429 backoff or provider queue. Never counted as agent latency. |

---

## 15. Definition of done — Phase A

- [ ] Agent drives macOS through `Environment` + `DesktopAdapter`; `replay_env` runs graph tests
- [ ] Provider swap verified: same suite runs on ≥2 providers by changing `.env` only
- [ ] Windows/Linux adapters present as explicit `NotImplementedError` stubs
- [ ] 15 tasks with programmatic checkers, sandbox reset works, tasks frozen
- [ ] Human reference recorded for all 15
- [ ] Baseline `RunResult` table committed, with variance from 3 repeat runs on a paid key
- [ ] `llm_calls_per_step` at or near 1.0; `schema_repair_rate` recorded
- [ ] Latency reported as `agent_latency_s`, with `provider_wait_s` shown separately
- [ ] Latency-vs-step curve plotted
- [ ] Full trajectories persisted for every run
- [ ] LangSmith tracing live
- [ ] Guardrails enforced (app allowlist, sandbox, approval tiers, step cap, budgets, rpm)
- [ ] Demo video recorded
- [ ] **Accessibility permission revoked from Claude.app** — granted for Phase A
      development only (A0). It is machine-wide: mouse, keyboard, and the UI of every
      other app. Revoke in System Settings → Privacy & Security → Accessibility.

**Then, and only then, Phase B begins.**

---

## 16. GitHub metadata

**Description:** A computer-use agent — same success rate, a fraction of the latency. Any vision LLM, any OS.

**Topics:** `computer-use-agent` `llm-agents` `gui-automation` `langgraph` `gemini`
`agent-benchmarks` `vision-language-models` `accessibility-api` `macos`

The README's headline claim, once measured:
*"X× lower latency and Y% lower cost than the baseline, at equal success rate on 15 tasks."*

Include the ablation table, the calibration plot, the latency-vs-step curve, the
steps-vs-human column, the provider/model comparison, and a **failures section**.
The failures section is not optional — it is the credibility.

---

## 17. Deferred: OSWorld

Not abandoned — descoped. It was never the thesis; it was the anchor that would let the
claim read *"as accurate as the published field."* Without it, results are measured against
our own frozen baseline and against human cost, which is honest but unanchored.

It cannot run on this machine: Apple Silicon, no `/dev/kvm`, 8 GB RAM, and an x86_64 guest
image. Adding it later means a rented Linux host (or OSWorld's AWS provider) plus one new
file, `env/osworld_env.py`, behind the existing ABC — and `desktop/linux.py`, which the
platform seam already accounts for.

**This is why there are three seams and why coordinates, actions, observations and model
calls are all backend-neutral.** Keeping them clean costs nothing now and is the whole
price of admission later. Do not collapse them.
