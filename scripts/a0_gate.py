"""A0 permissions gate — CLAUDE.md §9 A0.

Four checks. No project code, no agent, nothing persisted outside this dir.
1. screencapture writes a PNG          -> Screen Recording permission
2. AX tree is non-empty                -> Accessibility permission
3. cursor moves                        -> Accessibility permission
4. capture:point ratio, MEASURED       -> CLAUDE.md §5.1
"""
import os
import subprocess
import sys
import tempfile

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)


print("=" * 78)
print("A0 PERMISSIONS GATE")
print("=" * 78)

# --- who is asking? macOS grants TCC per-executable, so this matters ---------
print("\n[0] process identity (macOS attributes permissions to one of these)")
print(f"  python     : {os.path.realpath(sys.executable)}")
chain, pid = [], os.getpid()
for _ in range(6):
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not out:
            break
        ppid, comm = out.split(None, 1)
        chain.append(comm.strip())
        pid = int(ppid)
        if pid <= 1:
            break
    except Exception:
        break
print(f"  ancestry   : {' <- '.join(chain)}")

# --- 1. screen capture ------------------------------------------------------
print("\n[1] Screen Recording — screencapture")
shot = os.path.join(tempfile.gettempdir(), "a0_shot.png")
cap_w = cap_h = 0
try:
    r = subprocess.run(
        ["screencapture", "-x", "-t", "png", shot],
        capture_output=True, text=True, timeout=20,
    )
    if r.returncode != 0:
        record("screencapture", False, f"exit {r.returncode}: {r.stderr.strip()[:120]}")
    elif not os.path.exists(shot) or os.path.getsize(shot) == 0:
        record("screencapture", False, "no file written (permission likely denied)")
    else:
        from PIL import Image

        with Image.open(shot) as im:
            cap_w, cap_h = im.size
        kb = os.path.getsize(shot) // 1024
        record("screencapture", True, f"{cap_w}x{cap_h} px, {kb} KB")
except Exception as e:
    record("screencapture", False, f"{type(e).__name__}: {e}")

# --- screen geometry in POINTS ---------------------------------------------
print("\n[2] screen geometry (points) — AppKit")
pt_w = pt_h = 0
backing = 0.0
try:
    from AppKit import NSScreen

    scr = NSScreen.mainScreen()
    fr = scr.frame()
    pt_w, pt_h = int(fr.size.width), int(fr.size.height)
    backing = float(scr.backingScaleFactor())
    record("NSScreen.frame", True, f"{pt_w}x{pt_h} pt, backingScaleFactor={backing}")
except Exception as e:
    record("NSScreen.frame", False, f"{type(e).__name__}: {e}")

# --- 3. accessibility tree --------------------------------------------------
print("\n[3] Accessibility — AXUIElement")
try:
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementSetMessagingTimeout,
    )
    from AppKit import NSWorkspace

    trusted = bool(AXIsProcessTrusted())
    print(f"        AXIsProcessTrusted() = {trusted}")

    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    app_name = front.localizedName()
    bundle_id = front.bundleIdentifier()
    app_pid = front.processIdentifier()
    print(f"        frontmost = {app_name!r}  bundle={bundle_id}  pid={app_pid}")

    app_ref = AXUIElementCreateApplication(app_pid)
    AXUIElementSetMessagingTimeout(app_ref, 0.5)

    err, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
    if err != 0 or not windows:
        record("AX tree", False, f"AXWindows err={err}, windows={windows!r}")
    else:
        # shallow walk: count nodes, sample roles
        roles: dict[str, int] = {}
        total = 0

        def walk(node, depth=0):
            global total
            if depth > 6 or total > 400:
                return
            total += 1
            e, role = AXUIElementCopyAttributeValue(node, "AXRole", None)
            if e == 0 and role:
                roles[str(role)] = roles.get(str(role), 0) + 1
            e, kids = AXUIElementCopyAttributeValue(node, "AXChildren", None)
            if e == 0 and kids:
                for k in kids:
                    walk(k, depth + 1)

        walk(windows[0])
        top = sorted(roles.items(), key=lambda kv: -kv[1])[:6]
        record("AX tree", total > 1,
               f"{total} nodes from {app_name!r}; top roles: "
               + ", ".join(f"{r}x{n}" for r, n in top))
except Exception as e:
    record("AX tree", False, f"{type(e).__name__}: {e}")

# --- 4. cursor control ------------------------------------------------------
print("\n[4] Accessibility — cursor movement (pyautogui)")
try:
    import pyautogui

    pyautogui.FAILSAFE = False  # probe only; the real executor sets this True
    x0, y0 = pyautogui.position()
    pyautogui.moveTo(x0 + 12, y0 + 12, duration=0)
    x1, y1 = pyautogui.position()
    pyautogui.moveTo(x0, y0, duration=0)
    moved = (x1, y1) != (x0, y0)
    record("cursor move", moved, f"({x0},{y0}) -> ({x1},{y1}) -> restored")
    print(f"        pyautogui.size() = {pyautogui.size()}  (should be POINTS)")
except Exception as e:
    record("cursor move", False, f"{type(e).__name__}: {e}")

# --- 5. THE RATIO -----------------------------------------------------------
print("\n[5] capture:point ratio — MEASURED, not assumed (CLAUDE.md §5.1)")
if cap_w and pt_w:
    rx, ry = cap_w / pt_w, cap_h / pt_h
    print(f"        capture {cap_w}x{cap_h} px  /  screen {pt_w}x{pt_h} pt")
    print(f"        ratio   x={rx:.4f}  y={ry:.4f}")
    print(f"        backingScaleFactor reports {backing}")
    consistent = abs(rx - ry) < 0.01
    record("ratio", consistent, f"{rx:.4f} (x/y agree: {consistent})")
    if abs(rx - backing) > 0.01:
        print(f"        >>> CONFIRMED: backingScaleFactor ({backing}) != true ratio "
              f"({rx:.4f}). Assuming it would put every click off by "
              f"{abs(1 - backing / rx) * 100:.1f}%.")
    else:
        print(f"        >>> ratio equals backingScaleFactor — display is in native mode.")
else:
    record("ratio", False, "need both capture and point dimensions")

if os.path.exists(shot):
    os.remove(shot)

# --- verdict ----------------------------------------------------------------
print("\n" + "=" * 78)
passed = sum(1 for _, ok, _ in RESULTS if ok)
for name, ok, _ in RESULTS:
    print(f"  [{'x' if ok else ' '}] {name}")
print(f"\nA0 GATE: {passed}/{len(RESULTS)} checks passed")
print("=" * 78)
sys.exit(0 if passed == len(RESULTS) else 1)
