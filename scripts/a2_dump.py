"""A2 gate (CLAUDE.md §9): annotated screenshots across 10 apps, verified by eye.

Writes runs/a2_som/<app>.png plus a stats line per app. The numbers matter as
much as the pictures: element count is prompt tokens on every single step, and
`sparse` is the OCR trigger we instrumented instead of pre-building.
"""

import subprocess
import sys
import time
from pathlib import Path

from AppKit import NSWorkspace

from os_agent.desktop.macos import MacOSAdapter
from os_agent.perception.elements import to_elements
from os_agent.perception.som import annotate

APPS = [
    "TextEdit", "Finder", "Calculator", "System Settings",
    "Preview", "Safari", "Notes", "Calendar", "Reminders", "Maps",
]

OUT = Path("runs/a2_som")


# Finder is always running and cannot be quit; it activates by being handed
# something to show.
_UNQUITTABLE = {"Finder": lambda: subprocess.run(["open", str(Path.home())], check=False)}


def activate(name: str) -> str:
    """Force an app to the front.

    MEASURED A2: `open -a` only activates a FRESH LAUNCH. For an
    already-running app it is a no-op, and NSRunningApplication
    .activateWithOptions_ returns True while doing nothing — macOS blocks a
    background process from stealing focus once a foreground app holds it, and
    the API lies about it. The only reliable lever we have without Apple Events
    permission is to quit the app and let the launch activate it.
    """
    ws = NSWorkspace.sharedWorkspace()
    if name in _UNQUITTABLE:
        _UNQUITTABLE[name]()
    else:
        subprocess.run(["pkill", "-x", name], capture_output=True, check=False)
        time.sleep(1.0)
        subprocess.run(["open", "-a", name], capture_output=True, check=False)

    for _ in range(16):
        time.sleep(0.45)
        front = ws.frontmostApplication()
        if front and (front.localizedName() or "").lower() == name.lower():
            return str(front.localizedName())
    front = ws.frontmostApplication()
    return f"!{front.localizedName()}" if front else ""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ad = MacOSAdapter()
    print(f"{'app':<18} {'raw':>5} {'els':>5} {'walk':>7} {'som':>7}  {'note':<12}")
    print("-" * 68)
    rows = []
    for name in APPS:
        front = activate(name)
        if not front:
            print(f"{name:<18} {'—':>5} {'—':>5}  not available")
            continue
        png, _ = ad.capture()
        t = time.perf_counter()
        nodes = ad.raw_tree()
        walk = (time.perf_counter() - t) * 1000
        els = to_elements(nodes, app=front)
        t = time.perf_counter()
        out = annotate(png, els)
        som = (time.perf_counter() - t) * 1000
        (OUT / f"{name.replace(' ', '_')}.png").write_bytes(out)
        note = "SPARSE" if len(els) < 3 else ("dense" if len(els) > 60 else "")
        # Quit what we just captured: a foreground app holding focus is what
        # blocks the NEXT app from activating (see activate()).
        if name != "Finder":
            subprocess.run(["pkill", "-x", name], capture_output=True, check=False)
            time.sleep(0.8)
        print(
            f"{front[:17]:<18} {len(nodes):>5} {len(els):>5} "
            f"{walk:>6.0f}ms {som:>6.0f}ms  {note:<12}"
        )
        rows.append((front, len(nodes), len(els), walk))

    if rows:
        print("-" * 68)
        avg_els = sum(r[2] for r in rows) / len(rows)
        avg_walk = sum(r[3] for r in rows) / len(rows)
        print(f"{'mean':<18} {'':>5} {avg_els:>5.0f} {avg_walk:>6.0f}ms   "
              f"~{avg_els * 15:.0f} prompt tokens/step")
    return 0


if __name__ == "__main__":
    sys.exit(main())
