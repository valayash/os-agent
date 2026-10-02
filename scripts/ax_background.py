"""Step 5 of the AX plan: does the AX path work with the app BEHIND?

CLAUDE.md §5.3 proved that every way of bringing an app to the FRONT is
blocked during unattended automation. It did not test acting on an app that
stays behind. AXPress and AXValue go to the target process directly — no
window server, no cursor, no focus — so macOS may have no reason to block
them. The reference repo reports took_focus == False on Calculator every run.

Do not take its word for it on this machine. This script:

  1. opens Calculator (and, with --textedit, a sandbox document in TextEdit)
  2. asks YOU to click some other app so the targets are behind
  3. presses Calculator buttons and writes a TextEdit value BY PID, recording
     the frontmost app before and after every single operation
  4. reads the result back from the tree and writes runs/ax_background.json

PASS means: every operation landed AND took_focus is False for all of them.
The answer decides how much of §5.3 and §5.4 can be rewritten. Run it twice,
once right after touching the machine and once after ~10 idle minutes — §5.3's
lesson is that the variable is how recently a human touched the machine.

    python scripts/ax_background.py
    python scripts/ax_background.py --textedit
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from AppKit import NSWorkspace

from os_agent.actions.executor import Executor
from os_agent.config import settings
from os_agent.desktop.macos import MacOSAdapter
from os_agent.perception.elements import to_elements
from os_agent.types import Action

OUT = Path("runs/ax_background.json")
CALC = "com.apple.calculator"
TEXTEDIT = "com.apple.TextEdit"

# Calculator labels differ between macOS versions; the first match wins.
CANDIDATES = {
    "clear": ("All Clear", "Clear", "AC", "C"),
    "one": ("1", "one"),
    "plus": ("Add", "+", "plus"),
    "two": ("2", "two"),
    "equals": ("Equals", "=", "equals"),
}


def pid_of(bundle: str) -> int | None:
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if str(app.bundleIdentifier() or "") == bundle:
            return int(app.processIdentifier())
    return None


def wait_pid(bundle: str, timeout: float = 8.0) -> int | None:
    end = time.time() + timeout
    while time.time() < end:
        if pid := pid_of(bundle):
            return pid
        time.sleep(0.25)
    return None


def find(elements, names):
    for want in names:
        for e in elements:
            if e.name.strip().lower() == want.lower() and "press" in e.ops:
                return e
    return None


def calc_display(ad: MacOSAdapter, pid: int) -> str:
    nodes = ad.tree_for_pid(pid)
    texts = [n.name for n in nodes if n.role == "AXStaticText" and n.name]
    return texts[-1] if texts else ""


def op(ad, ex, label, fn, results, target_bundle):
    before = ad.frontmost_app()
    err = ""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — record, never hide
        err = f"{type(exc).__name__}: {exc}"[:200]
    after = ad.frontmost_app()
    rec = {
        "op": label,
        "front_before": before[1],
        "front_after": after[1],
        "took_focus": after[1] != before[1] or after[1] == target_bundle,
        "receipts": [r.__dict__ for r in ex.receipts],
        "error": err,
    }
    results.append(rec)
    print(f"  {label:<26} front {before[0]!r:>14} -> {after[0]!r:<14} "
          f"took_focus={rec['took_focus']}  {err or 'ok'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--textedit", action="store_true",
                    help="also write a value into a sandbox TextEdit document")
    a = ap.parse_args()

    subprocess.run(["open", "-a", "Calculator"], capture_output=True)
    doc = None
    if a.textedit:
        settings.sandbox.mkdir(parents=True, exist_ok=True)
        doc = settings.sandbox / "ax_background.txt"
        doc.write_text("this line should be replaced\n")
        subprocess.run(["open", "-a", "TextEdit", str(doc)], capture_output=True)

    calc_pid = wait_pid(CALC)
    te_pid = wait_pid(TEXTEDIT) if a.textedit else None
    if calc_pid is None or (a.textedit and te_pid is None):
        print("ABORT: target app did not start")
        return 2

    input("\n  Click ANY other app (e.g. Finder) so the targets are BEHIND it,\n"
          "  then come back here and press Enter... ")
    time.sleep(0.5)

    ad = MacOSAdapter()
    front = ad.frontmost_app()
    # The terminal you pressed Enter in is now in front, which is fine — what
    # matters is that it is not a target.
    if front[1] in (CALC, TEXTEDIT):
        print(f"ABORT: a target is frontmost ({front[0]}). It must be behind.")
        return 2
    print(f"\n  frontmost at start: {front[0]} ({front[1]})\n")

    # EXEC_MODE=ax regardless of .env: this IS the AX path under test.
    ex = Executor(ad, approval_on=False, exec_mode="ax")
    results: list[dict] = []

    els = to_elements(ad.tree_for_pid(calc_pid))
    picked = {k: find(els, v) for k, v in CANDIDATES.items()}
    missing = [k for k, e in picked.items() if e is None]
    if missing:
        print(f"ABORT: no pressable Calculator button for {missing}. Seen:")
        print("  ", sorted({e.name for e in els if "press" in e.ops})[:60])
        return 2

    for key in ("clear", "one", "plus", "two", "equals"):
        e = picked[key]
        op(ad, ex, f"calc press {e.name!r}",
           lambda e=e: ex.run([Action(kind="click", element_id=e.id)], els,
                              mode="freeform", allowed_bundles=None),
           results, CALC)
    display = calc_display(ad, calc_pid)
    calc_ok = "3" in display
    print(f"\n  Calculator display reads {display!r}  -> {'3 as expected' if calc_ok else 'WRONG'}")

    te_ok = None
    if te_pid is not None:
        tels = to_elements(ad.tree_for_pid(te_pid))
        area = next((e for e in tels if e.role == "textarea" and "type" in e.ops), None)
        if area is None:
            print("  TextEdit: no writable text area found in the tree")
            te_ok = False
        else:
            text = "written in the background"
            op(ad, ex, "textedit set_value",
               lambda: ex.run([Action(kind="set_value", element_id=area.id, text=text)],
                              tels, mode="freeform", allowed_bundles=None),
               results, TEXTEDIT)
            got = ad.read_value(area.path)
            te_ok = got is not None and got.rstrip("\n") == text
            print(f"  TextEdit read back {got!r}  -> {'ok' if te_ok else 'WRONG'}")

    stole = [r["op"] for r in results if r["took_focus"]]
    errors = [r["op"] for r in results if r["error"]]
    passed = not stole and not errors and calc_ok and te_ok is not False
    verdict = {
        "passed": passed, "took_focus_on": stole, "errors_on": errors,
        "calculator_display": display, "textedit_ok": te_ok,
        "front_at_start": front[1], "results": results,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(verdict, indent=2, default=str))

    print("\n" + "=" * 68)
    print(f"  {'PASS' if passed else 'FAIL'} — took focus on: {stole or 'nothing'}; "
          f"errors on: {errors or 'nothing'}")
    print(f"  written {OUT}")
    print("=" * 68)
    if doc is not None:
        print(f"  (left {doc} open and unsaved — close TextEdit without saving)")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
