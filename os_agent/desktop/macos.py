"""macOS adapter — the only implemented platform in Phase A (CLAUDE.md §8.2).

    frontmost:  the window server's front-to-back order (NOT NSWorkspace, see
                frontmost_app)
    capture:    CGDisplayCreateImage, falling back to the screencapture CLI
    tree:       AX walk of the frontmost app's focused window + top-level menu bar
    keyboard:   CGEvent with explicit flags  — NOT pyautogui, see below
    mouse:      pyautogui (this half is fine)
    ocr:        deferred — raises (CLAUDE.md §8.2)
    AX exec:    AXUIElementPerformAction / AXUIElementSetAttributeValue — the
                EXEC_MODE=ax path (CLAUDE.md §4.11). Straight into the target
                process; no window server, no cursor, no focus change.
"""

import io
import os
import subprocess
import tempfile
import time

import pyautogui
import Quartz
import structlog
from AppKit import NSScreen, NSWorkspace
from ApplicationServices import (
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementIsAttributeSettable,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
    AXValueCreate,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from PIL import Image

from os_agent.config import settings
from os_agent.desktop.base import NotOnThisPlatform
from os_agent.types import RawNode, Receipt

# Slam the cursor into a screen corner to abort everything. The only kill
# switch that works while the agent is mid-action (CLAUDE.md §8.5).
log = structlog.get_logger(__name__)

pyautogui.FAILSAFE = True

# ---------------------------------------------------------------------------
# Keyboard: MEASURED A1 — why this is not pyautogui.
#
# pyautogui sends a modifier KEY-DOWN and lets the app infer the chord. On
# macOS that is unreliable: hotkey("command", "a") arrives as a literal "a", so
# Select All silently becomes typing a character and cmd+s silently stops
# saving. Fast typing also drops characters — "Reviewed" arrived as "Rviw".
# Every one of those failures is SILENT. No exception, just wrong input that
# looks exactly like the model having chosen badly.
#
# CGEvent with explicit flags is correct. The part that must not be forgotten
# is RELEASING the modifier (flags=0 on its key-up): leave it set and Command
# stays latched, so every following character becomes a menu shortcut and the
# text simply never appears.
# ---------------------------------------------------------------------------
_TAP = Quartz.kCGHIDEventTap

_MODS: dict[str, tuple[int, int]] = {
    "command": (55, Quartz.kCGEventFlagMaskCommand),
    "shift": (56, Quartz.kCGEventFlagMaskShift),
    "option": (58, Quartz.kCGEventFlagMaskAlternate),
    "control": (59, Quartz.kCGEventFlagMaskControl),
}

_ALIASES = {
    "cmd": "command", "meta": "command", "super": "command", "win": "command",
    "alt": "option", "ctrl": "control",
    "esc": "escape", "del": "delete", "return": "enter",
}

# macOS virtual keycodes (Carbon kVK_*). Only what the action space needs.
_KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "enter": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "home": 115, "pageup": 116, "forwarddelete": 117, "end": 119,
    "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
}

# Measured 13.9 ms/char end to end. Below ~6 ms the target app starts dropping
# events, and it drops them silently.
_EVENT_GAP_S = 0.006

_LTR_MARK = "‎"

# Perception cost controls (CLAUDE.md §8.2).
_AX_TIMEOUT_S = 0.5
# Depth cap: settings.ax_max_depth (AX_MAX_DEPTH, default 60). 20 was the A2
# value, kept as a name so scripts/ax_depth.py can compare the two.
_MAX_DEPTH_A2 = 20
_MAX_NODES = 300
_MAX_NAME = 120

# Containers: never offered an operation, so their capabilities are not worth
# two extra IPC round-trips each. A cost control, like pruning — not a filter:
# the node is still emitted.
_CONTAINER_ROLES = {
    "AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea", "AXLayoutItem",
    "AXWindow", "AXSheet", "AXToolbar", "AXTabGroup", "AXList", "AXOutline",
    "AXTable", "AXColumn", "AXBrowser", "AXUnknown", "AXSplitter", "AXWebArea",
    "AXStaticText", "AXImage",
}

# AX error codes worth naming (AXError.h).
_AX_ERR_CANNOT_COMPLETE = -25204  # the app never replied: it MAY have acted

# Menu walk caps. Menus are walked eagerly only on the AX path, cached per pid.
_MENU_MAX_DEPTH = 3  # bar item > menu item > submenu item
_MENU_MAX_ITEMS = 400

# Path handles: "w:<pid>:0/3/1" (child indices under the window) and
# "m:<pid>:Format<TAB>Make Rich Text" (titles under the menu bar).
_SEP = "\t"


def _attr(node, name: str):
    err, value = AXUIElementCopyAttributeValue(node, name, None)
    return value if err == 0 else None


def _bbox(node) -> tuple[int, int, int, int] | None:
    """AXPosition / AXSize are WRAPPED AXValue objects, not plain structs.

    Reading node.x or node.width raises, silently producing empty geometry for
    every node while roles and names still look correct. AXValueGetValue is the
    unwrap. This cost an hour at A0; it is why the comment is this long.
    """
    pos_v, size_v = _attr(node, "AXPosition"), _attr(node, "AXSize")
    if pos_v is None or size_v is None:
        return None
    ok_p, pos = AXValueGetValue(pos_v, kAXValueCGPointType, None)
    ok_s, size = AXValueGetValue(size_v, kAXValueCGSizeType, None)
    if not (ok_p and ok_s):
        return None
    x, y = int(pos.x), int(pos.y)
    return x, y, x + int(size.width), y + int(size.height)


def _main_window(app_ref):
    """The window the walk, the fit and the AX handles all agree on.

    The focused window first, as §8.2 specifies — AXWindows[0] is only usually
    the front one, and with two Finder windows open "usually" is the bug. Then
    the main window, then the first listed, for apps that report neither.
    ONE function so a handle recorded by the walk resolves against the same
    window it was read from.
    """
    for attr in ("AXFocusedWindow", "AXMainWindow"):
        if (w := _attr(app_ref, attr)) is not None:
            return w
    windows = _attr(app_ref, "AXWindows")
    return windows[0] if windows else None


def _capabilities(node, role: str) -> tuple[tuple[str, ...], bool]:
    """(action names, is AXValue settable). Two IPC reads; skipped on containers."""
    if role in _CONTAINER_ROLES:
        return (), False
    err, names = AXUIElementCopyActionNames(node, None)
    actions = tuple(str(n) for n in names) if err == 0 and names else ()
    err, settable = AXUIElementIsAttributeSettable(node, "AXValue", None)
    return actions, bool(settable) if err == 0 else False


def _clean(v) -> str:
    return str(v or "").replace(_LTR_MARK, "").strip()


def _source():
    return Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)


def _post(event, flags: int) -> None:
    Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(_TAP, event)
    time.sleep(_EVENT_GAP_S)


class MacOSAdapter:
    name = "macos"

    def __init__(self, *, max_depth: int | None = None) -> None:
        self.max_depth = settings.ax_max_depth if max_depth is None else max_depth
        # Menus are hundreds of IPC reads; titles we offer are the stable ones
        # (perception drops state-flipping titles), so walk once per app.
        self._menu_cache: dict[int, list[RawNode]] = {}

    # -- geometry ----------------------------------------------------------
    def screen_size_points(self) -> tuple[int, int]:
        frame = NSScreen.mainScreen().frame()
        return int(frame.size.width), int(frame.size.height)

    # Capture has two paths. MEASURED A2: CGDisplayCreateImage is deprecated on
    # macOS 14+ and returns None intermittently — it did so mid-run on this
    # machine while Screen Recording was definitely granted (A0 passed and the
    # previous capture in the same process had just succeeded). Allocating a
    # 20.7 MB framebuffer every few seconds on 8 GB is the likely trigger.
    #
    # So: try the fast in-process path, then fall back to the `screencapture`
    # CLI, which is slower (subprocess + disk) but is the known-good path from
    # A0. A capture that raises kills a whole benchmark run; a capture that is
    # 200 ms slower costs 200 ms. Count the fallbacks so the rate is visible.
    fallback_captures = 0

    def _grab(self) -> Image.Image:
        for _ in range(2):
            img = Quartz.CGDisplayCreateImage(Quartz.CGMainDisplayID())
            if img is not None:
                px_w = Quartz.CGImageGetWidth(img)
                px_h = Quartz.CGImageGetHeight(img)
                bpr = Quartz.CGImageGetBytesPerRow(img)
                raw = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
                return Image.frombuffer("RGBA", (px_w, px_h), bytes(raw), "raw", "BGRA", bpr, 1)
            time.sleep(0.15)

        MacOSAdapter.fallback_captures += 1
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            path = fh.name
        try:
            r = subprocess.run(
                ["screencapture", "-x", "-t", "png", "-D", "1", path],
                capture_output=True, timeout=20, check=False,
            )
            if r.returncode != 0 or not os.path.getsize(path):
                raise RuntimeError(
                    "Both capture paths failed. CGDisplayCreateImage returned None twice "
                    f"and screencapture exited {r.returncode}. Check Screen Recording "
                    "permission with scripts/a0_gate.py."
                )
            return Image.open(path).convert("RGBA").copy()
        finally:
            if os.path.exists(path):
                os.remove(path)

    def capture(self) -> tuple[bytes, float]:
        """(PNG at POINT resolution, measured capture:point ratio).

        CGDisplayCreateImage returns the FRAMEBUFFER, which is neither the panel
        resolution nor necessarily backingScaleFactor x points: A0 measured a
        2880x1800 framebuffer over 1440x900 points on a 2560x1600 panel. The
        ratio is measured every capture and nothing hardcodes it (§5.1).
        """
        pil = self._grab()
        px_w, px_h = pil.size

        pt_w, pt_h = self.screen_size_points()
        ratio = px_w / pt_w
        if pil.size != (pt_w, pt_h):
            # Measured: LANCZOS 50 ms, reduce(2) 10.6 ms. For an exact integer
            # ratio reduce() is a box average over each NxN block — the correct
            # downsample, not a cheaper approximation. LANCZOS stays as the
            # fallback for fractional ratios (a scaled display mode).
            if ratio > 1 and float(ratio).is_integer() and px_h / pt_h == ratio:
                pil = pil.reduce(int(ratio))
            else:
                pil = pil.resize((pt_w, pt_h), Image.LANCZOS)

        buf = io.BytesIO()
        pil.convert("RGB").save(buf, format="PNG", compress_level=1)
        return buf.getvalue(), ratio

    # -- context -----------------------------------------------------------
    def frontmost_app(self) -> tuple[str, str]:
        """(display_name, bundle_id) — from the WINDOW SERVER, not NSWorkspace.

        MEASURED A5, over three failed runs before it was caught:
        NSWorkspace.frontmostApplication() NEVER UPDATES inside this process.
        It returns the app that spawned our Python — Claude — no matter what is
        actually in front. Probably because a non-GUI process has no run loop
        pumping workspace notifications.

        The damage was total and silent:

            NSWorkspace says "Claude"
              -> raw_tree() walks Claude's tree (9 menu items)
              -> the element list describes the wrong app
              -> the model has nothing to pick, so every action is raw coords
              -> it guesses pixel positions from the screenshot

        Every action across three runs used element_id=None. The agent was
        never once looking at the app it was driving, and reflection — which
        reads the SCREENSHOT — kept correctly saying "you are in Safari" while
        the element list insisted otherwise.

        The window server's own front-to-back ordering is authoritative and
        always correct. Use it, and keep NSWorkspace only to resolve the
        bundle id from the pid.
        """
        pid = self._frontmost_window_pid()
        if pid:
            for app in NSWorkspace.sharedWorkspace().runningApplications():
                if app.processIdentifier() == pid:
                    name = str(app.localizedName() or "").replace(_LTR_MARK, "").strip()
                    return name, str(app.bundleIdentifier() or "")

        # No normal window on screen (menu open, Spotlight up, desktop focused).
        # Fall back rather than return nothing, and say so.
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return "", ""
        log.debug("frontmost.fallback", note="no on-screen window; using NSWorkspace")
        return (str(app.localizedName() or "").replace(_LTR_MARK, "").strip(),
                str(app.bundleIdentifier() or ""))

    def _frontmost_window_pid(self) -> int | None:
        """PID owning the front-most normal window, per the window server."""
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        for w in info:
            if w.get("kCGWindowLayer", 1) != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            if b.get("Width", 0) < 200 or b.get("Height", 0) < 120:
                continue
            return int(w.get("kCGWindowOwnerPID", 0)) or None
        return None

    # -- input -------------------------------------------------------------
    def click(self, x: int, y: int, kind: str = "click") -> None:
        if kind == "double_click":
            pyautogui.doubleClick(x, y)
        elif kind == "right_click":
            pyautogui.rightClick(x, y)
        else:
            pyautogui.click(x, y)

    def type_text(self, text: str) -> None:
        """Unicode-safe: sets the event's unicode string, no keycode table."""
        src = _source()
        for ch in text:
            if ch in ("\n", "\r"):
                self.press_keys(["enter"])
                continue
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(src, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                _post(ev, 0)

    def press_keys(self, keys: list[str]) -> None:
        names = [_ALIASES.get(k.lower(), k.lower()) for k in keys]
        mods = [n for n in names if n in _MODS]
        plain = [n for n in names if n not in _MODS]
        if len(plain) != 1:
            raise ValueError(f"press_keys needs exactly one non-modifier key, got {keys!r}")
        key = plain[0]
        if key not in _KEYCODES:
            raise ValueError(f"no macOS keycode for {key!r}; add it to _KEYCODES")

        code = _KEYCODES[key]
        mask = 0
        for m in mods:
            mask |= _MODS[m][1]

        src = _source()
        for m in mods:
            _post(Quartz.CGEventCreateKeyboardEvent(src, _MODS[m][0], True), mask)
        _post(Quartz.CGEventCreateKeyboardEvent(src, code, True), mask)
        _post(Quartz.CGEventCreateKeyboardEvent(src, code, False), mask)
        for m in reversed(mods):
            # flags=0 here is THE fix. Without it Command stays latched.
            _post(Quartz.CGEventCreateKeyboardEvent(src, _MODS[m][0], False), 0)

    def scroll(self, x: int, y: int, amount: int) -> None:
        pyautogui.scroll(amount, x=x, y=y)

    # -- window management (PREFLIGHT ONLY, never in the action space) ------
    #
    # CLAUDE.md §5.3: focus cannot be taken programmatically during unattended
    # automation, so app switching is NOT an agent action. But the human is at
    # the keyboard at the moment they press Enter on the CLI, and that is
    # exactly the window in which macOS still honours an activation request.
    # So we spend it once, before the loop starts, and then VERIFY.
    #
    # §13: and while we are here, fit the window to the screen. A default-size
    # TextEdit window captured 2,393 KB because ~97% of the frame was desktop
    # wallpaper; maximised it was 86 KB. 28x, free, every single step.

    def activate_app(self, name: str, *, timeout_s: float = 6.0) -> bool:
        """Bring `name` forward and CONFIRM it arrived. Never trust the return
        code of an activation call on macOS — every mechanism in §5.3's table
        returned success while doing nothing."""
        target = name.strip().lower()
        running = None
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            label = str(app.localizedName() or "").replace(_LTR_MARK, "").strip()
            if label.lower() == target:
                running = app
                break

        if running is not None:
            running.activateWithOptions_(1 << 1)  # ActivateIgnoringOtherApps
        else:
            # Not running: launch it. `open -a` also carries the activation.
            subprocess.run(["open", "-a", name], capture_output=True)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            front, _ = self.frontmost_app()
            if front.strip().lower() == target:
                return True
            time.sleep(0.25)
        return False

    def fit_frontmost_window(self) -> bool:
        """Resize the front window to fill the visible screen area.

        MAXIMISE, NEVER FULLSCREEN (§13). The green button gives the app its
        own Space, and a separate Space is the §5.3 wall: every other window
        becomes unrenderable and the agent goes blind. Setting the frame keeps
        one Space and gets all of the benefit.
        """
        pid = self._frontmost_window_pid()
        if pid is None:
            return False
        ref = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(ref, _AX_TIMEOUT_S)
        window = _main_window(ref)
        if window is None:
            return False

        # visibleFrame excludes the menu bar and the Dock, wherever the Dock
        # is. AppKit's origin is BOTTOM-left; AX wants TOP-left, so the y flip
        # is not optional — get it wrong and the window lands under the Dock.
        screen = NSScreen.mainScreen()
        full, vis = screen.frame(), screen.visibleFrame()
        top_inset = full.size.height - (vis.origin.y + vis.size.height)

        pos = AXValueCreate(kAXValueCGPointType,
                            Quartz.CGPoint(vis.origin.x, top_inset))
        size = AXValueCreate(kAXValueCGSizeType,
                             Quartz.CGSize(vis.size.width, vis.size.height))
        # Position first, then size: a window pinned at the old origin can be
        # clamped by the screen edge and silently come back smaller.
        e1 = AXUIElementSetAttributeValue(window, "AXPosition", pos)
        e2 = AXUIElementSetAttributeValue(window, "AXSize", size)
        ok = e1 == 0 and e2 == 0
        log.info("window.fit", ok=ok, pos_err=e1, size_err=e2,
                 w=int(vis.size.width), h=int(vis.size.height))
        return ok

    # -- perception (A2) ---------------------------------------------------
    def raw_tree(self) -> list[RawNode]:
        """Walk the focused window of the frontmost app into flat RawNodes.

        Unfiltered on purpose: filtering and numbering are platform-neutral and
        live in perception/elements.py (CLAUDE.md §4.10).

        Four cost controls, which are design and not optimization — every
        attribute read below is an IPC round-trip into another process:
          1. focused window only, never the whole desktop
          2. depth cap + node cap
          3. prune zero-size / offscreen subtrees BEFORE descending
          4. 0.5s messaging timeout so one hung app cannot stall a step
        """
        # The window server, NOT NSWorkspace. Fixing frontmost_app() alone was
        # not enough: this walk had its own copy of the same broken call, so
        # perception kept reading the spawning app's tree while the reporter
        # said the right thing. Two call sites, one truth — keep them together.
        pid = self._target_pid()
        return [] if pid is None else self.tree_for_pid(pid)

    def _target_pid(self) -> int | None:
        pid = self._frontmost_window_pid()
        if pid is None:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return None
            pid = app.processIdentifier()
        return pid

    def tree_for_pid(self, pid: int, *, max_depth: int | None = None) -> list[RawNode]:
        """raw_tree() for ANY app by pid — no focus needed (§5.3 scoping).

        The agent always uses the frontmost app; scripts/ax_background.py and
        scripts/ax_depth.py use this directly.
        """
        depth_cap = self.max_depth if max_depth is None else max_depth
        ref = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(ref, _AX_TIMEOUT_S)

        out: list[RawNode] = []
        sw, sh = self.screen_size_points()

        # The menu bar, TOP LEVEL ONLY.
        # Found at A2 by looking at an annotated screenshot: walking AXWindows
        # alone leaves File / Edit / Format / View unnumbered, and on macOS the
        # menu bar is how most things are actually done — Save As, Make Plain
        # Text, Export. An agent that cannot see it cannot drive a Mac app.
        # We do NOT descend into the menus: that is hundreds of IPC round-trips
        # for items that are not on screen. Clicking a top-level item opens the
        # menu, and its contents appear in the NEXT observation as a real
        # window. One cheap hop instead of one expensive eager walk.
        err, menubar = AXUIElementCopyAttributeValue(ref, "AXMenuBar", None)
        if err == 0 and menubar:
            err, items = AXUIElementCopyAttributeValue(menubar, "AXChildren", None)
            if err == 0 and items:
                for item in items:
                    bbox = _bbox(item)
                    if bbox is None:
                        continue
                    title = _clean(_attr(item, "AXTitle"))
                    out.append(
                        RawNode(
                            role=str(_attr(item, "AXRole") or "AXMenuBarItem"),
                            name=title[:_MAX_NAME],
                            bbox=bbox,
                            enabled=True,
                            depth=1,
                            actions=("AXPress",),
                            path=f"m:{pid}:{title}",
                        )
                    )

        if (window := _main_window(ref)) is not None:
            self._walk(window, 0, out, sw, sh, pid=pid, idx=(), depth_cap=depth_cap)
        return out

    def _walk(self, node, depth: int, out: list[RawNode], sw: int, sh: int, *,
              pid: int, idx: tuple[int, ...], depth_cap: int) -> None:
        if depth > depth_cap or len(out) >= _MAX_NODES:
            return

        role = _attr(node, "AXRole")
        if role is None:
            return

        bbox = _bbox(node)
        # Prune BEFORE descending: a window scrolled off-screen can hold
        # hundreds of children, and each one costs IPC to even look at.
        if bbox is not None:
            x0, y0, x1, y1 = bbox
            if x1 <= 0 or y1 <= 0 or x0 >= sw or y0 >= sh:
                return
            if (x1 - x0) <= 0 or (y1 - y0) <= 0:
                return

        name = ""
        for a in ("AXTitle", "AXDescription", "AXValue"):
            v = _attr(node, a)
            if v:
                name = str(v).replace(_LTR_MARK, "").strip()
                break

        enabled = _attr(node, "AXEnabled")
        actions, settable = _capabilities(node, str(role))
        out.append(
            RawNode(
                role=str(role),
                name=name[:_MAX_NAME],
                bbox=bbox,
                enabled=True if enabled is None else bool(enabled),
                focused=bool(_attr(node, "AXFocused") or False),
                depth=depth,
                actions=actions,
                value_settable=settable,
                path=f"w:{pid}:{'/'.join(map(str, idx))}",
            )
        )

        children = _attr(node, "AXChildren")
        if children:
            for i, child in enumerate(children):
                self._walk(child, depth + 1, out, sw, sh,
                           pid=pid, idx=idx + (i,), depth_cap=depth_cap)

    # -- AX execution path (EXEC_MODE=ax, CLAUDE.md §4.11) ------------------
    #
    # UNVERIFIED ON THIS MACHINE. The claim that these need no focus comes from
    # max1874/jev-computer-use (took_focus == False on Calculator).
    # scripts/ax_background.py is the test; until it has run here, §5.3 stands
    # as written for both paths.

    def _resolve(self, path: str):
        """Handle -> live AXUIElement, or None. Walks the same child indices
        the perception walk recorded, so a reordered tree resolves to a
        DIFFERENT node — which is why the executor guards on role + name."""
        try:
            kind, pid_s, rest = path.split(":", 2)
            pid = int(pid_s)
        except ValueError:
            return None
        ref = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(ref, _AX_TIMEOUT_S)
        if kind == "w":
            node = _main_window(ref)
            if node is None:
                return None
            for i in (int(x) for x in rest.split("/") if x != ""):
                kids = _attr(node, "AXChildren")
                if not kids or i >= len(kids):
                    return None
                node = kids[i]
            return node
        if kind == "m":
            node = _attr(ref, "AXMenuBar")
            for depth, title in enumerate(rest.split(_SEP)):
                if node is None:
                    return None
                if depth > 0:  # bar item / menu item -> its AXMenu
                    menus = _attr(node, "AXChildren")
                    node = menus[0] if menus else None
                    if node is None:
                        return None
                kids = _attr(node, "AXChildren") or []
                node = next((k for k in kids if _clean(_attr(k, "AXTitle")) == title), None)
            return node
        return None

    def read_node(self, path: str) -> RawNode | None:
        node = self._resolve(path)
        if node is None:
            return None
        role = _attr(node, "AXRole")
        if role is None:
            return None
        name = ""
        for a in ("AXTitle", "AXDescription", "AXValue"):
            if v := _attr(node, a):
                name = _clean(v)
                break
        if path.startswith("m:"):
            name = " > ".join(path.split(":", 2)[2].split(_SEP))
        actions, settable = _capabilities(node, str(role))
        enabled = _attr(node, "AXEnabled")
        return RawNode(
            role=str(role), name=name[:_MAX_NAME], bbox=_bbox(node),
            enabled=True if enabled is None else bool(enabled),
            focused=bool(_attr(node, "AXFocused") or False),
            actions=actions, value_settable=settable, path=path,
        )

    def read_value(self, path: str) -> str | None:
        node = self._resolve(path)
        if node is None:
            return None
        v = _attr(node, "AXValue")
        return None if v is None else str(v)

    def _window_sig(self, path: str) -> tuple:
        """Coarse 'did the window move?' — title + top-level child count."""
        try:
            pid = int(path.split(":", 2)[1])
        except (ValueError, IndexError):
            return ()
        ref = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(ref, _AX_TIMEOUT_S)
        wins = _attr(ref, "AXWindows") or []
        main = _main_window(ref)
        if main is None:
            return (0,)
        return (len(wins), _clean(_attr(main, "AXTitle")),
                len(_attr(main, "AXChildren") or []))

    def _perform(self, path: str, action: str, mechanism: str) -> Receipt:
        node = self._resolve(path)
        if node is None:
            return Receipt(mechanism, dispatched=False, detail="handle no longer resolves")
        before = self._window_sig(path)
        err = AXUIElementPerformAction(node, action)
        if err == _AX_ERR_CANNOT_COMPLETE:
            # No reply inside the timeout. The app may well have acted.
            return Receipt(mechanism, dispatched=True, timed_out=True,
                           detail=f"{action}: no reply in {_AX_TIMEOUT_S}s")
        if err != 0:
            return Receipt(mechanism, dispatched=False, detail=f"{action} -> AXError {err}")
        time.sleep(0.15)
        return Receipt(mechanism, dispatched=True,
                       window_changed=self._window_sig(path) != before)

    def press(self, path: str) -> Receipt:
        return self._perform(path, "AXPress", "ax_press")

    def invoke_menu(self, path: str) -> Receipt:
        """Invoke a menu command with the menu still CLOSED. One step, not two.
        Note an INACTIVE app reports every menu item disabled; the receipt
        says what AX answered, it does not pretend."""
        return self._perform(path, "AXPress", "ax_menu")

    def set_value(self, path: str, text: str) -> Receipt:
        """Write, then READ BACK. Never trust the error code alone.

        A rich-text composer can INSERT rather than replace on AXValue, so a
        clean return code proves nothing. If the read-back disagrees and the
        element is focused in the frontmost app, fall back to erase-by-keys
        then typing, and say so in the receipt — which mechanism ran is part
        of the result.
        """
        node = self._resolve(path)
        if node is None:
            return Receipt("ax_set_value", dispatched=False, detail="handle no longer resolves")
        err = AXUIElementSetAttributeValue(node, "AXValue", text)
        if err == _AX_ERR_CANNOT_COMPLETE:
            return Receipt("ax_set_value", dispatched=True, timed_out=True,
                           detail="no reply; the value MAY have been written")
        got = self.read_value(path)
        if err == 0 and got is not None and got.rstrip("\n") == text:
            return Receipt("ax_set_value", dispatched=True, verified=True)

        # Fallback: keyboard. Aimed ONLY at an element confirmed focused in the
        # frontmost app — keystrokes go wherever focus is, and erasing the
        # wrong field is the one thing worse than not writing this one.
        pid = int(path.split(":", 2)[1])
        focused = bool(_attr(node, "AXFocused") or False)
        if not (focused and self._frontmost_window_pid() == pid):
            return Receipt("ax_set_value", dispatched=err == 0, verified=False,
                           detail=f"read back {str(got)[:40]!r} (err {err}); "
                                  "not focused, keyboard fallback refused")
        self.press_keys(["command", "a"])
        self.press_keys(["delete"])
        self.type_text(text)
        got = self.read_value(path)
        return Receipt("keys_fallback", dispatched=True,
                       verified=got is not None and got.rstrip("\n") == text,
                       detail=f"AXValue refused or inserted (err {err})")

    def menu_tree(self) -> list[RawNode]:
        pid = self._target_pid()
        if pid is None:
            return []
        if pid not in self._menu_cache:
            self._menu_cache[pid] = self._walk_menus(pid)
        return self._menu_cache[pid]

    def _walk_menus(self, pid: int) -> list[RawNode]:
        ref = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(ref, _AX_TIMEOUT_S)
        bar = _attr(ref, "AXMenuBar")
        out: list[RawNode] = []
        if bar is None:
            return out

        def walk(container, titles: list[str]) -> None:
            if len(titles) > _MENU_MAX_DEPTH or len(out) >= _MENU_MAX_ITEMS:
                return
            for item in _attr(container, "AXChildren") or []:
                if len(out) >= _MENU_MAX_ITEMS:
                    return
                title = _clean(_attr(item, "AXTitle"))
                if not title:  # separators
                    continue
                chain = titles + [title]
                sub = _attr(item, "AXChildren") or []
                if sub:  # has a submenu: descend, do not emit the parent
                    walk(sub[0], chain)
                    continue
                if len(chain) < 2:
                    continue
                enabled = _attr(item, "AXEnabled")
                out.append(RawNode(
                    role="AXMenuItem", name=" > ".join(chain)[:_MAX_NAME], bbox=None,
                    enabled=True if enabled is None else bool(enabled),
                    actions=("AXPress",), path=f"m:{pid}:{_SEP.join(chain)}",
                ))

        for bar_item in _attr(bar, "AXChildren") or []:
            title = _clean(_attr(bar_item, "AXTitle"))
            menus = _attr(bar_item, "AXChildren") or []
            if title and menus:
                walk(menus[0], [title])
        return out

    def ocr(self, image_png: bytes, bbox: tuple[int, int, int, int]) -> str:
        # A2: VNRecognizeTextRequest (Apple Vision) — on-device, Neural Engine,
        # far better on UI text than Tesseract and no extra binary.
        raise NotOnThisPlatform("ocr is deferred (CLAUDE.md §8.2).")
