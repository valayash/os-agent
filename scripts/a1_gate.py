"""A1 gate (CLAUDE.md §9).

  "a hardcoded script opens TextEdit, types a line, saves to the sandbox, and
   a checker confirms the file — through the Environment interface, with
   policy enforced."

No LLM. No perception. No graph. The question this answers is only:
can we drive the machine through our own seams, with the guardrails on?
"""

import asyncio
import sys

from os_agent.actions.policy import Verdict
from os_agent.desktop.macos import MacOSAdapter
from os_agent.env.base import ApprovalDenied, PolicyViolation
from os_agent.env.desktop_env import DesktopEnv
from os_agent.telemetry.log import configure
from os_agent.types import Action

configure(pretty=True)


def auto_approve(action: Action, verdict: Verdict) -> bool:
    """A1 harness only. run.py gets a real prompt at A4."""
    print(f"    APPROVE (auto, A1 harness): {action.kind:<12} — {verdict.reason}")
    return True


async def main() -> int:
    env = DesktopEnv(MacOSAdapter(), mode="bench", approve=auto_approve)

    print("\n[1] reset -> teardown, fresh sandbox, setup, observe")
    obs = await env.reset("textedit_append")
    print(f"    frontmost : {obs.meta['app']!r} ({obs.meta['bundle_id']})")
    print(f"    screenshot: {len(obs.screenshot)//1024} KB, scale={obs.meta['scale']}")
    print(f"    perception: {obs.meta['perception']}")

    if obs.meta["bundle_id"] != "com.apple.TextEdit":
        print("    !! TextEdit is not frontmost — policy will refuse. Aborting cleanly.")
        env.close()
        return 1

    print("\n[2] act -> end of document, append a line, save")
    await env.act([
        Action(kind="key", keys=["cmd", "down"]),
        Action(kind="type", text="\nReviewed"),
        Action(kind="key", keys=["cmd", "s"]),
    ])

    await asyncio.sleep(1.0)

    print("\n[3] evaluate -> programmatic checker on END STATE")
    score = env.evaluate()
    print(f"    score = {score}")

    print("\n[4] policy must REFUSE an action outside the allowlist")
    env.task = env.task.__class__(**{**env.task.__dict__, "apps": ["com.apple.Safari"]})
    try:
        await env.act([Action(kind="key", keys=["cmd", "s"])])
        print("    !! policy did NOT refuse — gate fails")
        refused = False
    except PolicyViolation as e:
        print(f"    refused: {str(e)[:90]}...")
        refused = True
    except ApprovalDenied:
        refused = False

    env.close()
    ok = score == 1.0 and refused
    print("\n" + "=" * 70)
    print(f"A1 GATE: {'PASS' if ok else 'FAIL'}  (checker={score}, policy_refused={refused})")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
