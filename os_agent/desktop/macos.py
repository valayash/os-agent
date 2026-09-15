"""macOS adapter — the only implemented platform in Phase A (CLAUDE.md §8.2).

    frontmost:  NSWorkspace.frontmostApplication()
    capture:    CGDisplayCreateImage (in-process; no subprocess, no disk)
    keyboard:   CGEvent with explicit flags  — NOT pyautogui, see below
    mouse:      pyautogui (this half is fine)
    tree / ocr: A2
"""

import io
import time

import pyautogui
import Quartz
from AppKit import NSScreen, NSWorkspace
from PIL import Image

from os_agent.desktop.base import NotOnThisPlatform
from os_agent.types import RawNode

# Slam the cursor into a screen corner to abort everything. The only kill
# switch that works while the agent is mid-action (CLAUDE.md §8.5).
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

    def capture(self) -> tuple[bytes, float]:
        """(PNG at POINT resolution, measured capture:point ratio).

        CGDisplayCreateImage returns the FRAMEBUFFER, which is neither the panel
        resolution nor necessarily backingScaleFactor x points: A0 measured a
        2880x1800 framebuffer over 1440x900 points on a 2560x1600 panel. The
        ratio is measured every capture and nothing hardcodes it (§5.1).
        """
        img = Quartz.CGDisplayCreateImage(Quartz.CGMainDisplayID())
        if img is None:
            raise RuntimeError(
                "CGDisplayCreateImage returned None — Screen Recording permission "
                "is probably not granted. Run scripts/a0_gate.py."
            )
        px_w = Quartz.CGImageGetWidth(img)
        px_h = Quartz.CGImageGetHeight(img)
        bpr = Quartz.CGImageGetBytesPerRow(img)
        raw = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
        pil = Image.frombuffer("RGBA", (px_w, px_h), bytes(raw), "raw", "BGRA", bpr, 1)

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
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return "", ""
        name = str(app.localizedName() or "")
        bundle = str(app.bundleIdentifier() or "")
        # WhatsApp and others prefix titles with U+200E LEFT-TO-RIGHT MARK.
        return name.replace(_LTR_MARK, "").strip(), bundle

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

    # -- A2 ----------------------------------------------------------------
    def raw_tree(self) -> list[RawNode]:
        # GOTCHA found at A0, recorded so A2 does not rediscover it:
        # AXPosition and AXSize do NOT return plain points. They return wrapped
        # AXValue objects and node.x / node.width silently raise. Unwrap with
        #     ok, pt = AXValueGetValue(v, kAXValueCGPointType, None)
        # Reading them directly is why a naive dump shows empty geometry for
        # every node while still printing correct roles and names.
        raise NotOnThisPlatform("raw_tree is A2 (CLAUDE.md §9). A1 needs no perception.")

    def ocr(self, image_png: bytes, bbox: tuple[int, int, int, int]) -> str:
        # A2: VNRecognizeTextRequest (Apple Vision) — on-device, Neural Engine,
        # far better on UI text than Tesseract and no extra binary.
        raise NotOnThisPlatform("ocr is A2 (CLAUDE.md §9).")
