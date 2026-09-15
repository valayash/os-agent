"""Linux adapter — NOT IMPLEMENTED (CLAUDE.md §3, §8.2, §17).

The seam exists; the implementation does not. Claiming cross-platform without
a measured suite on this platform is the same sin as editing a benchmark task
after the baseline (CLAUDE.md §2).

When someone fills this in: AT-SPI via pyatspi is the weakest of the three
accessibility APIs on modern desktops (Wayland breaks much of it), but Linux
is the only platform with real VM snapshot resets — which is what OSWorld
relies on, and why §17 keeps this seam clean.

It also needs its OWN task suite and its OWN baseline.
"""

from os_agent.desktop.base import NotOnThisPlatform

_MSG = "Linux adapter is not implemented. See CLAUDE.md §8.2 and §17."


class LinuxAdapter:
    name = "linux"

    def __getattr__(self, item: str):
        raise NotOnThisPlatform(f"{_MSG} (called {item!r})")
