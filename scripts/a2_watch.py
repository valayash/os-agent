"""A2 gate helper: capture + annotate whatever is frontmost, on a timer.

macOS will not let a background process change focus during unattended
automation (CLAUDE.md §5.3), so the human drives: click between apps while
this runs, and it captures each one. Writes runs/a2_som/<App>.png.
"""

import sys
import time
from pathlib import Path

from os_agent.desktop.macos import MacOSAdapter
from os_agent.perception.elements import to_elements
from os_agent.perception.som import annotate

OUT = Path("runs/a2_som")
INTERVAL_S = 4.0


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    OUT.mkdir(parents=True, exist_ok=True)
    ad = MacOSAdapter()
    seen: dict[str, int] = {}
    print(f"Click between apps for {seconds}s. Capturing every {INTERVAL_S:.0f}s.\n")
    print(f"{'app':<22} {'raw':>5} {'els':>5} {'walk':>7}  {'note':<10}")
    print("-" * 56)

    deadline = time.time() + seconds
    while time.time() < deadline:
        name, bundle = ad.frontmost_app()
        if not name:
            time.sleep(INTERVAL_S)
            continue
        png, _ = ad.capture()
        t = time.perf_counter()
        nodes = ad.raw_tree()
        walk = (time.perf_counter() - t) * 1000
        els = to_elements(nodes, app=bundle)
        if name not in seen or len(els) > seen[name]:
            seen[name] = len(els)
            (OUT / f"{name.replace(' ', '_').replace('/', '_')}.png").write_bytes(
                annotate(png, els) if els else png
            )
            note = "SPARSE" if len(els) < 3 else ("dense" if len(els) > 60 else "")
            print(f"{name[:21]:<22} {len(nodes):>5} {len(els):>5} {walk:>6.0f}ms  {note:<10}")
        time.sleep(INTERVAL_S)

    print("-" * 56)
    print(f"{len(seen)} distinct apps captured -> {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
