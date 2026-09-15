"""Windows adapter — NOT IMPLEMENTED (CLAUDE.md §3, §8.2).

The seam exists; the implementation does not. Claiming cross-platform without
a measured suite on this platform is the same sin as editing a benchmark task
after the baseline (CLAUDE.md §2).

When someone fills this in: UI Automation via pywinauto/comtypes is the richest
and fastest of the three accessibility APIs, and needs no permission prompts.
It also needs its OWN task suite and its OWN baseline — TextEdit and Finder do
not exist here.
"""

from os_agent.desktop.base import NotOnThisPlatform

_MSG = "Windows adapter is not implemented. See CLAUDE.md §8.2."


class WindowsAdapter:
    name = "windows"

    def __getattr__(self, item: str):
        raise NotOnThisPlatform(f"{_MSG} (called {item!r})")
