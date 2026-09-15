"""A2 gate helper: capture + annotate whatever is frontmost, on a timer.

macOS will not let a background process change focus during unattended
automation (CLAUDE.md §5.3), so the human drives: click between apps while this
runs. Writes runs/a2_som/<App>.png.

Every iteration is isolated — one bad capture must not kill a 60-second run.
"""

import sys
import time
import traceback
from pathlib import Path

from os_agent.desktop.macos import MacOSAdapter
from os_agent.perception.elements import to_elements
from os_agent.perception.som import annotate

OUT = Path("runs/a2_som")
INTERVAL_S = 3.0


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    OUT.mkdir(parents=True, exist_ok=True)
    ad = MacOSAdapter()
    best: dict[str, int] = {}
    errors = 0

    print(f"\n  CLICK BETWEEN APPS for the next {seconds}s.")
    print("  Finder, Safari, Calculator, System Settings, Notes, Preview, Mail...")
    print(f"  Capturing whatever is frontmost every {INTERVAL_S:.0f}s.\n")
    print(f"  {'left':>5}  {'app':<22} {'raw':>5} {'els':>5} {'walk':>7}  note")
    print("  " + "-" * 60)

    deadline = time.time() + seconds
    while time.time() < deadline:
        left = int(deadline - time.time())
        try:
            name, bundle = ad.frontmost_app()
            if not name:
                time.sleep(INTERVAL_S)
                continue
            png, _ = ad.capture()
            t = time.perf_counter()
            nodes = ad.raw_tree()
            walk = (time.perf_counter() - t) * 1000
            els = to_elements(nodes, app=bundle)

            if len(els) > best.get(name, -1):
                best[name] = len(els)
                safe = name.replace(" ", "_").replace("/", "_")
                (OUT / f"{safe}.png").write_bytes(annotate(png, els) if els else png)
                note = "SPARSE" if len(els) < 3 else ("dense" if len(els) > 60 else "")
                print(f"  {left:>4}s  {name[:21]:<22} {len(nodes):>5} {len(els):>5} "
                      f"{walk:>6.0f}ms  {note}")
        except Exception:  # noqa: BLE001 — one bad frame must not end the run
            errors += 1
            if errors <= 2:
                traceback.print_exc(limit=1)
        time.sleep(INTERVAL_S)

    print("  " + "-" * 60)
    print(f"  {len(best)} apps captured -> {OUT}/   "
          f"(capture fallbacks: {MacOSAdapter.fallback_captures}, errors: {errors})")
    for app, n in sorted(best.items(), key=lambda kv: -kv[1]):
        print(f"    {app:<24} {n:>3} elements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
