"""Does a newline inside type_text arrive as a RETURN KEYPRESS?

The whole eight-message incident rests on that question, and it was argued
rather than measured. This measures it, in TextEdit, where a Return is
harmless and visible.

    typed:  "aaa" + chr(10) + "bbb"

    two lines in the document -> the newline WAS delivered as Return.
                                 In WhatsApp, Return is Send. Hypothesis holds.
    one line  "aaabbb"        -> it was swallowed. I am wrong; look elsewhere.

Run it yourself: focus only works from a terminal a human just touched (§5.3).
"""

import time

from os_agent.desktop.macos import MacOSAdapter
from os_agent.perception.elements import to_elements

a = MacOSAdapter()
if not a.activate_app("TextEdit"):
    raise SystemExit("could not focus TextEdit — click it once, then re-run")
time.sleep(1.0)

probe = "aaa" + chr(10) + "bbb"
print(f"typing {probe!r}  ({len(probe)} chars, one of them U+000A)")
a.type_text(probe)
time.sleep(0.8)

values = [e.name for e in to_elements(a.raw_tree()) if "aaa" in e.name]
if not values:
    raise SystemExit("nothing typed — is a TextEdit document open and focused?")

got = values[0]
print(f"document now contains: {got!r}")
print()
if chr(10) in got:
    print("NEWLINE DELIVERED AS RETURN.  In a chat window that is Send.")
else:
    print("newline swallowed. The send came from something else — I was wrong.")
