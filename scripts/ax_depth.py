"""Step 1 of the AX plan: is "Electron exposes nothing" our depth cap?

CLAUDE.md §8.2 recorded Claude Desktop as 8 nodes, all AXGroup/AXWindow. The
reference repo (max1874/jev-computer-use) made the same claim and then found
Chromium puts the web area ~9 levels under the window and the interface
another 10-20 below that: Feishu went from 2 characters at depth 18 to 2,431
at depth 40. Our cap was 20.

For every app with a normal on-screen window, walk its tree at the OLD cap and
the NEW cap and record both side by side. No focus needed: the walk is by pid.

    python scripts/ax_depth.py                 # 20 vs settings.ax_max_depth
    python scripts/ax_depth.py --old 20 --new 60 --repeat 3

Output: a table on stdout and runs/ax_depth.json. The table goes into §8.4
with BOTH columns — the old number is evidence, not something to overwrite.
"""

import argparse
import json
import statistics
import time
from pathlib import Path

import Quartz

from os_agent.config import settings
from os_agent.desktop.macos import _LTR_MARK, _MAX_DEPTH_A2, MacOSAdapter
from os_agent.perception.elements import to_elements

OUT = Path("runs/ax_depth.json")
SKIP_OWNERS = {"WindowManager", "Dock", "Window Server", "Control Centre",
               "Control Center", "Spotlight", "loginwindow", "Notification Centre",
               "Notification Center"}


def apps_with_windows() -> dict[int, str]:
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    out: dict[int, str] = {}
    for w in info:
        if w.get("kCGWindowLayer", 1) != 0:
            continue
        b = w.get("kCGWindowBounds", {})
        if b.get("Width", 0) < 200 or b.get("Height", 0) < 120:
            continue
        owner = str(w.get("kCGWindowOwnerName", "")).replace(_LTR_MARK, "").strip()
        if owner in SKIP_OWNERS:
            continue
        out.setdefault(int(w["kCGWindowOwnerPID"]), owner)
    return out


def measure(ad: MacOSAdapter, pid: int, depth: int, repeat: int) -> dict:
    times, nodes = [], []
    for _ in range(repeat):
        t0 = time.perf_counter()
        nodes = ad.tree_for_pid(pid, max_depth=depth)
        times.append((time.perf_counter() - t0) * 1000)
    els = to_elements(nodes)
    return {
        "raw_nodes": len(nodes),
        "elements": len(els),
        "actionable": sum(1 for e in els if e.ops),
        "text_chars": sum(len(n.name) for n in nodes),
        "max_depth_seen": max((n.depth for n in nodes), default=0),
        "ms_median": round(statistics.median(times), 1),
        "hit_node_cap": len(nodes) >= 300,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=int, default=_MAX_DEPTH_A2)
    ap.add_argument("--new", type=int, default=settings.ax_max_depth)
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()

    ad = MacOSAdapter()
    rows = []
    for pid, owner in sorted(apps_with_windows().items(), key=lambda kv: kv[1]):
        old = measure(ad, pid, a.old, a.repeat)
        new = measure(ad, pid, a.new, a.repeat)
        rows.append({"app": owner, "pid": pid, f"d{a.old}": old, f"d{a.new}": new})

    hdr = (f"{'app':<22} {'raw':>11} {'elements':>11} {'actionable':>11} "
           f"{'text chars':>13} {'ms':>13}  cap?")
    print(f"depth {a.old} -> {a.new}\n{hdr}\n{'-' * len(hdr)}")
    for r in rows:
        o, n = r[f"d{a.old}"], r[f"d{a.new}"]

        def pair(k, o=o, n=n):
            return f"{o[k]:>5}->{n[k]:<5}"
        print(f"{r['app'][:22]:<22} {pair('raw_nodes')} {pair('elements')} "
              f"{pair('actionable')} {o['text_chars']:>6}->{n['text_chars']:<6} "
              f"{o['ms_median']:>6}->{n['ms_median']:<6} "
              f"{'NODE CAP' if n['hit_node_cap'] else ''}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"old": a.old, "new": a.new, "rows": rows}, indent=2))
    print(f"\nwritten {OUT}")
    print("A row that hits NODE CAP at the new depth is not measured, it is truncated: "
          "the 300-node cap fired first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
