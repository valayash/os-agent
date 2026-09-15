# scripts/

Gate checks and diagnostics. Not part of the agent — nothing here is imported
by `os_agent/`.

| Script | Gate | What it proves |
|---|---|---|
| `a0_gate.py` | A0 | Screen Recording + Accessibility granted, and the capture:point ratio measured (CLAUDE.md §5.1) |

## A0 notes

macOS grants Accessibility to the **responsible app bundle**, not the calling binary.
Claude Code runs from its own nested bundle:

    .venv/bin/python <- zsh <- .../claude-code/<ver>/claude.app/Contents/MacOS/claude

so the grant must be on **`com.anthropic.claude-code`** (shown in System Settings as
lowercase `claude`, grid icon) — NOT `com.anthropic.claudefordesktop` ("Claude",
starburst icon). Two bundles, near-identical names, one letter of case apart.

Re-run after any macOS update, Claude Code version bump (the bundle path contains the
version), or display resolution change.
