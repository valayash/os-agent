"""macOS adapter — the only implemented platform in Phase A (CLAUDE.md §8.2).

    frontmost:  NSWorkspace.frontmostApplication()
    capture:    CGDisplayCreateImage (in-process; no subprocess, no disk)
    keyboard:   CGEvent with explicit flags  — NOT pyautogui, see below
    mouse:      pyautogui (this half is fine)
    tree / ocr: A2
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
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementSetMessagingTimeout,
    AXValueGetValue,
    kAXValueCGPointType,
    kAXValueCGSizeType,
)
from PIL import Image

from os_agent.desktop.base import NotOnThisPlatform
from os_agent.types import RawNode

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
_MAX_DEPTH = 20
_MAX_NODES = 300
_MAX_NAME = 120


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


def _source():
    return Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)


def _post(event, flags: int) -> None:
    Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(_TAP, event)
    time.sleep(_EVENT_GAP_S)


class MacOSAdapter:
    name = "macos"

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
        """(display_name, bundle_id), cross-checked against the window server.

        MEASURED A5, the hard way: NSWorkspace.frontmostApplication() reported
        'Claude' for an entire run while the agent's keystrokes were reaching
        WhatsApp — the message was actually sent, twice. Input followed the
        real keyboard focus; this API reported the app on OUR Space.

        Perception and input disagreeing SILENTLY is the worst failure this
        system can have (§8.5). So we ask the window server too, and when the
        two disagree we say so rather than picking a winner: a caller that
        knows the observation is untrustworthy can refuse to act on it, which
        is strictly better than one that acts confidently on the wrong screen.
        """
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        ns_name = str(app.localizedName() or "") if app else ""
        ns_bundle = str(app.bundleIdentifier() or "") if app else ""
        ns_name = ns_name.replace(_LTR_MARK, "").strip()

        win_name = self._frontmost_window_owner()
        if win_name and ns_name and win_name != ns_name:
            log.warning(
                "frontmost.disagreement",
                nsworkspace=ns_name, window_server=win_name,
                note="observation may describe a different app than input reaches",
            )
        return ns_name, ns_bundle

    def _frontmost_window_owner(self) -> str:
        """Owner of the front-most normal window, per the window server."""
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
            return str(w.get("kCGWindowOwnerName", "")).replace(_LTR_MARK, "").strip()
        return ""

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

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

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
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return []
        ref = AXUIElementCreateApplication(app.processIdentifier())
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
                    title = _attr(item, "AXTitle")
                    out.append(
                        RawNode(
                            role=str(_attr(item, "AXRole") or "AXMenuBarItem"),
                            name=str(title or "").replace(_LTR_MARK, "").strip()[:_MAX_NAME],
                            bbox=bbox,
                            enabled=True,
                            depth=1,
                        )
                    )

        err, windows = AXUIElementCopyAttributeValue(ref, "AXWindows", None)
        if err == 0 and windows:
            self._walk(windows[0], 0, out, sw, sh)
        return out

    def _walk(self, node, depth: int, out: list[RawNode], sw: int, sh: int) -> None:
        if depth > _MAX_DEPTH or len(out) >= _MAX_NODES:
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
        out.append(
            RawNode(
                role=str(role),
                name=name[:_MAX_NAME],
                bbox=bbox,
                enabled=True if enabled is None else bool(enabled),
                focused=bool(_attr(node, "AXFocused") or False),
                depth=depth,
            )
        )

        children = _attr(node, "AXChildren")
        if children:
            for child in children:
                self._walk(child, depth + 1, out, sw, sh)

    def ocr(self, image_png: bytes, bbox: tuple[int, int, int, int]) -> str:
        # A2: VNRecognizeTextRequest (Apple Vision) — on-device, Neural Engine,
        # far better on UI text than Tesseract and no extra binary.
        raise NotOnThisPlatform("ocr is A2 (CLAUDE.md §9).")
