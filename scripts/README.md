# scripts/

Permission checks, phase gates and diagnostics. Not part of the agent — nothing here is
imported by `os_agent/`. Run from the repo root with the package installed
(`uv pip install -e '.[dev]'`).

| Script | Needs a Mac | What it does |
|---|---|---|
| `a0_gate.py` | yes | Checks Screen Recording and Accessibility permissions, that the cursor can be moved, and measures the capture:point ratio |
| `a1_gate.py` | yes | Opens TextEdit, types a line, saves into the sandbox and runs the checker — through the `Environment` interface with policy on. No model |
| `a2_dump.py` | yes | Writes annotated screenshots and element counts for a list of apps to `runs/a2_som/` |
| `a2_watch.py` | yes | Captures and annotates whatever app is in front every few seconds while you click between apps |
| `a2_windows.py` | yes | Annotates every on-screen window by pid, without needing focus |
| `a3_gate.py` | no | Drives the graph on the replay backend and checks all 7 exit paths |
| `a5_gate.py` | no | Checks reflection, budget abort, request pacing and trajectory files on the replay backend |
| `ax_depth.py` | yes | Walks each open app's tree at depth 20 and at the current `AX_MAX_DEPTH`; prints nodes, elements, text and time for both and writes `runs/ax_depth.json` |
| `ax_background.py` | yes | With Calculator (and with `--textedit`, a TextEdit document) behind another app, presses buttons and writes a value through the accessibility API, recording whether the app came to the front. Writes `runs/ax_background.json` |

`ax_depth.py` and `ax_background.py` have not been run yet.

## Permissions

macOS grants Accessibility to the **responsible app bundle**, not the calling binary.
Claude Code runs from its own nested bundle:

    .venv/bin/python <- zsh <- .../claude-code/<ver>/claude.app/Contents/MacOS/claude

so the grant must be on **`com.anthropic.claude-code`** (shown in System Settings as
lowercase `claude`, grid icon) — NOT `com.anthropic.claudefordesktop` ("Claude",
starburst icon). Two bundles, near-identical names, one letter of case apart.

Re-run `a0_gate.py` after any macOS update, Claude Code version bump (the bundle path
contains the version), or display resolution change.
