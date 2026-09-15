"""A2 gate, without needing focus.

macOS blocks programmatic focus changes (CLAUDE.md §5.3), but neither the
accessibility API nor window capture needs an app to be frontmost:
AXUIElementCreateApplication takes a pid, and CGWindowListCreateImage takes a
window id. So we walk every on-screen window in place.

Diagnostic only — the agent itself always works on the frontmost app. This
exists to verify that AX geometry lines up with what is actually rendered,
which is the one thing the A2 gate has to establish.

Boxes are offset into window-local coordinates: AX reports screen-global
points, and we are capturing one window, not the screen.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import Quartz
from PIL import Image

from os_agent.desktop.macos import _MAX_NODES, _attr, _bbox
from os_agent.perception.elements import to_elements
from os_agent.perception.som import annotate
from os_agent.types import RawNode

from ApplicationServices import (  # isort: skip
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementSetMessagingTimeout,
)

OUT = Path("runs/a2_som")
MIN_W, MIN_H = 260, 180

# System helper windows: real entries in the window list, no real UI. They are
# noise in a gate whose output a human has to read.
SKIP_OWNERS = {
    "WindowManager", "loginwindow", "Spotlight", "Wi-Fi", "Control Centre",
    "Control Center", "LocalAuthenticationRemoteService", "Open and Save Panel Service",
    "Notification Centre", "Notification Center", "Dock", "universalaccessd",
    "CoreServicesUIAgent", "TextInputMenuAgent", "Creative Cloud",
}


def is_blank(img: Image.Image) -> bool:
    """A window that launched but never drew renders solid black or white."""
    lo, hi = img.convert("L").resize((32, 32)).getextrema()
    return (hi - lo) < 12


def on_screen_windows() -> list[dict]:
    # NOT OnScreenOnly: a fullscreen app occupies its own Space, and every
    # other app's window is then "off screen" from here. CGWindowListCreateImage
    # can still render a window with a live backing store, so we list them all
    # and let the capture fail loudly for the ones that cannot be rendered.
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    out = []
    for w in info or []:
        if w.get("kCGWindowLayer", 1) != 0:  # 0 == normal app window
            continue
        b = w.get("kCGWindowBounds", {})
        if b.get("Width", 0) < MIN_W or b.get("Height", 0) < MIN_H:
            continue
        out.append(w)
    return out


def grab_window(win_id: int) -> Image.Image | None:
    """Capture one window by id.

    MEASURED A2: CGWindowListCreateImage returns None for nearly every window on
    macOS 15 — same deprecated family as CGDisplayCreateImage (§8.2). The
    `screencapture` CLI still renders them reliably, so that is the path we use
    here. Slower (subprocess + disk) but this is a diagnostic, not the loop.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        path = fh.name
    try:
        r = subprocess.run(
            ["screencapture", "-x", "-o", "-l", str(win_id), path],
            capture_output=True, timeout=20, check=False,
        )
        if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        return Image.open(path).convert("RGBA").copy()
    finally:
        if os.path.exists(path):
            os.remove(path)


def ax_nodes_for(pid: int, bounds: dict) -> list[RawNode]:
    """Walk the AX window whose origin matches this CG window's bounds."""
    ref = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(ref, 1.0)
    err, wins = AXUIElementCopyAttributeValue(ref, "AXWindows", None)
    if err != 0 or not wins:
        return []
    want = (int(bounds["X"]), int(bounds["Y"]))
    target = wins[0]
    for w in wins:
        bb = _bbox(w)
        if bb and abs(bb[0] - want[0]) <= 2 and abs(bb[1] - want[1]) <= 2:
            target = w
            break

    out: list[RawNode] = []

    def walk(node, depth=0):
        if depth > 20 or len(out) >= _MAX_NODES:
            return
        role = _attr(node, "AXRole")
        if role is None:
            return
        bb = _bbox(node)
        name = ""
        for a in ("AXTitle", "AXDescription", "AXValue"):
            v = _attr(node, a)
            if v:
                name = str(v).replace("‎", "").strip()
                break
        en = _attr(node, "AXEnabled")
        out.append(RawNode(str(role), name[:120], bb, True if en is None else bool(en),
                           False, depth))
        kids = _attr(node, "AXChildren")
        if kids:
            for k in kids:
                walk(k, depth + 1)

    walk(target)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    seen: dict[str, int] = {}
    print(f"  {'app':<24} {'raw':>5} {'els':>5} {'window':>13}  note")
    print("  " + "-" * 62)

    for w in on_screen_windows():
        owner = str(w.get("kCGWindowOwnerName", "?"))
        if owner in SKIP_OWNERS:
            continue
        pid = int(w.get("kCGWindowOwnerPID", 0))
        b = w.get("kCGWindowBounds", {})
        wx, wy = int(b["X"]), int(b["Y"])
        ww, wh = int(b["Width"]), int(b["Height"])

        img = grab_window(int(w["kCGWindowNumber"]))
        if img is None:
            print(f"  {owner[:23]:<24} {'—':>5} {'—':>5} {f'{ww}x{wh}':>13}  capture failed")
            continue
        # window image is in PIXELS; bring it to POINTS like capture() does
        ratio = img.width / ww if ww else 1
        if ratio > 1 and float(ratio).is_integer():
            img = img.reduce(int(ratio))
        elif img.size != (ww, wh):
            img = img.resize((ww, wh), Image.LANCZOS)

        if is_blank(img):
            print(f"  {owner[:23]:<24} {'—':>5} {'—':>5} {f'{ww}x{wh}':>13}  never drew")
            continue

        nodes = ax_nodes_for(pid, b)
        els = to_elements(nodes, app=owner)
        # AX reports screen-global points; this image is one window
        local = [type(e)(e.id, e.role, e.name,
                         (e.bbox[0] - wx, e.bbox[1] - wy, e.bbox[2] - wx, e.bbox[3] - wy),
                         e.enabled)
                 for e in els]
        local = [e for e in local if e.bbox[0] >= -4 and e.bbox[1] >= -4
                 and e.bbox[2] <= ww + 4 and e.bbox[3] <= wh + 4]

        if len(local) <= seen.get(owner, -1):
            continue
        seen[owner] = len(local)
        import io
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", compress_level=1)
        safe = owner.replace(" ", "_").replace("/", "_")
        png = annotate(buf.getvalue(), local) if local else buf.getvalue()
        (OUT / f"{safe}.png").write_bytes(png)
        note = "SPARSE" if len(local) < 3 else ("dense" if len(local) > 60 else "")
        print(f"  {owner[:23]:<24} {len(nodes):>5} {len(local):>5} {f'{ww}x{wh}':>13}  {note}")

    print("  " + "-" * 62)
    print(f"  {len(seen)} windows captured -> {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
